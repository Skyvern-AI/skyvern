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
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import SkyvernContextWindowExceededError
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.taskv3 import loop as loop_module
from skyvern.forge.taskv3.engine import MAX_TOKENS_CEILING, MAX_TOKENS_PER_ACTION_STEP, taskv3_runaway_backstops
from skyvern.forge.taskv3.loop import (
    ACTION_BUDGET_EXTENDED_EVENT,
    ACTION_BUDGET_EXTENSION_MAX_FACTOR,
    ACTION_BUDGET_EXTENSION_REFUSED_EVENT,
    ACTION_LOOP_NUDGE_AFTER,
    ACTION_LOOP_REASON_PREFIX,
    ACTION_LOOP_TERMINATE_AFTER,
    FAILURE_EVIDENCE_MIN_TOOL_CALLS,
    FAILURE_EVIDENCE_MIN_TURNS,
    FINAL_TURN_RELEASED_EVENT,
    NAV_DEAD_END_REASON_PREFIX,
    NAVIGATION_DEAD_END_STATUSES,
    NO_TOOL_CALL_NUDGE,
    PAGE_REFRESH_EXHAUSTED_REASON_PREFIX,
    PAGE_STATE_STALL_SHADOW_EVENT,
    PAGE_UNAVAILABLE_ERROR,
    PERCEPTION_REVISIT_EVENT,
    PERCEPTION_REVISIT_LOG_AFTER,
    PERCEPTION_RING,
    PERCEPTION_STALL_NUDGE_AFTER,
    PERCEPTION_STALL_REASON_PREFIX,
    PERCEPTION_STALL_SHADOW_EVENT,
    PERCEPTION_STALL_SUPPRESSED_EVENT,
    PERCEPTION_STALL_TERMINATE_AFTER,
    PROGRESS_LEDGER_SHADOW_EVENT,
    PROGRESS_LEDGER_WINDOW,
    ActivityRecency,
    LoopOutcome,
    SemanticCommitStats,
    SubmitWatch,
    ToolHandler,
    ToolResult,
    ToolSpec,
    _budget_extension_gate,
    _canonical_perception_content,
    _cap_trip_relieved,
    _PerceptionLedger,
    _ProgressEvidence,
    _ProgressLedger,
    _RevisitMemory,
    make_finish_tool,
    run_agent_tool_loop,
)
from skyvern.forge.taskv3.opaque_refs import mask_opaque_urls


class _ScriptedCaller:
    """Emits one queued turn per ``call``. Each turn is a list of (tool_name, args)."""

    def __init__(
        self,
        script: list[list[tuple[str, dict[str, Any]]]],
        texts: list[str] | None = None,
        reasoning_contents: list[str | None] | None = None,
    ) -> None:
        self._script = script
        # Per-turn assistant text, indexed like `script`; falls back to a fixed placeholder so
        # existing callers that don't care about the text still get a non-empty one.
        self._texts = texts
        # Per-turn message.reasoning_content, indexed like `script`. Mirrors the litellm
        # responses-bridge field a real gpt-5.6 call can return; unset by default so existing
        # callers see no reasoning_content key at all, matching a non-bridge response shape.
        self._reasoning_contents = reasoning_contents
        self.calls = 0
        self.message_history: list[dict[str, Any]] = []
        self.sent_tools: list[dict[str, Any]] | None = None
        # Model the real LLMCaller.llm_config the engine dereferences to gate the vision `look` tool.
        self.llm_config = SimpleNamespace(supports_vision=True)
        # Per-call record of the transient screenshots= arg the loop passed, and the image-block
        # count the built request would carry (message_history images + this turn's screenshots).
        self.screenshots_per_call: list[list[bytes] | None] = []
        self.image_blocks_per_call: list[int] = []

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
        screenshots: list[bytes] | None = None,
    ) -> dict[str, Any]:
        self.sent_tools = tools
        self.screenshots_per_call.append(list(screenshots) if screenshots else None)
        # The built request = the re-seeded transcript plus this turn's transient screenshots. Mirror
        # llm_messages_builder_with_history: history images (there should be none) + one block per
        # screenshot appended to a trailing user message.
        history_images = sum(
            1
            for msg in self.message_history
            for part in (msg.get("content") if isinstance(msg.get("content"), list) else [])
            if isinstance(part, dict) and part.get("type") in ("image_url", "image")
        )
        self.image_blocks_per_call.append(history_images + (len(screenshots) if screenshots else 0))
        idx = self.calls
        turn = self._script[idx] if idx < len(self._script) else []
        text = self._texts[idx] if self._texts and idx < len(self._texts) else "reasoning..."
        reasoning_content = (
            self._reasoning_contents[idx] if self._reasoning_contents and idx < len(self._reasoning_contents) else None
        )
        self.calls += 1
        message: dict[str, Any] = {"content": text}
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        if turn:
            message["tool_calls"] = [
                {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(turn)
            ]
        return {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _recording_tool(
    name: str, sink: list[tuple[str, dict[str, Any]]], *, raises: bool = False, billable: bool = False
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> ToolResult:
        sink.append((name, args))
        if raises:
            raise RuntimeError("boom")
        return ToolResult.ok(f"{name} done")

    return ToolSpec(
        name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler, billable=billable
    )


def _erroring_tool(
    name: str,
    sink: list[tuple[str, dict[str, Any]]],
    *,
    error_data: dict[str, Any] | None = None,
    billable: bool = False,
    recordable: bool = False,
) -> ToolSpec:
    """Like ``_recording_tool`` but returns ``ToolResult.error(...)`` directly (optionally carrying a
    ``data`` payload, e.g. ``page_transitioned``) instead of raising -- ``raises=True`` on
    ``_recording_tool`` only ever produces a bare ``tool_error: RuntimeError: boom`` with no data."""

    async def handler(args: dict[str, Any]) -> ToolResult:
        sink.append((name, args))
        return ToolResult.error(f"{name} failed", data=error_data)

    return ToolSpec(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        handler=handler,
        billable=billable,
        recordable=recordable,
    )


async def _run(
    script: list[list[tuple[str, dict[str, Any]]]],
    tools: list[ToolSpec],
    *,
    texts: list[str] | None = None,
    reasoning_contents: list[str | None] | None = None,
    **kwargs: Any,
):
    caller = _ScriptedCaller(script, texts=texts, reasoning_contents=reasoning_contents)
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


def _look_tool(
    sink: list[tuple[str, dict[str, Any]]], *, image: bytes = b"\x89PNG-fake", fail_before_renumbering: bool = False
) -> ToolSpec:
    """A `look`-shaped tool: returns a text legend AND a transient screenshot the loop must show the
    model on the next call only (never persisted to the transcript). Like the real tool it reports
    `marks_renumbered` once the manifest was rebuilt; a failure before that point reports nothing."""

    async def handler(args: dict[str, Any]) -> ToolResult:
        sink.append(("look", args))
        if fail_before_renumbering:
            return ToolResult.error("look budget reached")
        return ToolResult.ok("[1] button 'Next'", data={"marks_renumbered": True}, screenshots=[image])

    return ToolSpec(
        name="look",
        description="look",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )


@pytest.mark.asyncio
async def test_look_image_is_ephemeral_gone_the_turn_after() -> None:
    # Operator constraint: the annotated screenshot rides exactly ONE request (the turn after look)
    # and the request the turn AFTER that carries zero image blocks — the accumulation regression.
    look_calls: list[tuple[str, dict[str, Any]]] = []
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_look_tool(look_calls), _recording_tool("click", click_calls), make_finish_tool()]
    script = [
        [("look", {})],  # turn 1: model looks
        [("click", {"mark": 1})],  # turn 2: model acts on what it saw
        [("finish", {"status": "completed", "reason": "ok"})],  # turn 3
    ]
    outcome, caller = await _run(script, tools)

    assert outcome.status == "completed"
    # turn 1 request: no image yet; turn 2 request: the look image; turn 3 request: gone.
    assert caller.image_blocks_per_call[0] == 0
    assert caller.image_blocks_per_call[1] == 1
    assert caller.image_blocks_per_call[2] == 0
    # The transcript the loop re-seeds each turn never holds an image block.
    for msg in caller.message_history:
        content = msg.get("content")
        if isinstance(content, list):
            assert all(not (isinstance(p, dict) and p.get("type") in ("image_url", "image")) for p in content)


@pytest.mark.asyncio
async def test_n_looks_add_exactly_n_images_total() -> None:
    # Cost law: N looks add exactly N images across the whole run, not N x remaining turns.
    look_calls: list[tuple[str, dict[str, Any]]] = []
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_look_tool(look_calls), _recording_tool("click", click_calls), make_finish_tool()]
    script = [
        [("look", {})],
        [("click", {"mark": 1})],
        [("look", {})],
        [("click", {"mark": 2})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, caller = await _run(script, tools)

    assert outcome.status == "completed"
    assert len(look_calls) == 2
    total_images = sum(len(s) for s in caller.screenshots_per_call if s)
    assert total_images == 2
    # And no single request ever carries more than the one image just produced.
    assert max(caller.image_blocks_per_call) == 1


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
    # Deliberate contract change: the first trip grants one final observed turn instead of ending
    # the run immediately, so `reason` is now a human sentence (never the raw cap literal) and the
    # raw fact lives on `cap_trip`; the granted turn (turn 4) still had nothing but click to call.
    assert "max_turns" not in outcome.reason
    assert outcome.cap_trip == "max_turns (3) reached"
    assert outcome.turns == 4


@pytest.mark.asyncio
async def test_max_tool_calls_budget_exhausted() -> None:
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", click_calls), make_finish_tool()]
    script = [[("click", {}), ("click", {})]] * 10
    outcome, _ = await _run(script, tools, max_tool_calls=2)

    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the top-of-turn trip grants one final observed turn (an entire
    # batch here, since the script always calls two clicks per turn) before ending the run.
    assert "max_tool_calls" not in outcome.reason
    assert outcome.cap_trip == "max_tool_calls (2) reached"
    assert outcome.tool_calls == 4


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


def _navigate_tool(dead_end_status: int | None = None) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> ToolResult:
        data: dict[str, Any] = {"page_state_changed": True}
        if dead_end_status is not None:
            data["navigation_dead_end"] = dead_end_status
        return ToolResult.ok("navigated", data=data)

    return ToolSpec(
        name="navigate",
        description="navigate",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}},
        handler=handler,
    )


@pytest.mark.asyncio
async def test_navigate_dead_end_terminates_run() -> None:
    # A navigate that landed on a dead/removed posting (HTTP 404/410) must end the run as `terminated`,
    # matching v1 — NOT left to the model's finish choice (which defaults to failed at agent.py).
    tools = [_navigate_tool(dead_end_status=404), make_finish_tool()]
    script = [
        [("navigate", {"url": "https://jobs.example.test/acme/closed"})],
        [("finish", {"status": "completed", "reason": "should never run"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "terminated"
    assert outcome.reason.startswith(NAV_DEAD_END_REASON_PREFIX)


@pytest.mark.asyncio
async def test_navigate_without_dead_end_does_not_terminate() -> None:
    # Anti-over-route: an ordinary navigation (no dead-end signal) must NOT be reclassified — the run
    # proceeds and finishes on the model's own verdict.
    tools = [_navigate_tool(dead_end_status=None), make_finish_tool()]
    script = [
        [("navigate", {"url": "https://jobs.example.test/acme/123"})],
        [("finish", {"status": "completed", "reason": "applied"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert outcome.reason == "applied"


@pytest.mark.asyncio
async def test_batched_dead_end_then_live_navigate_recovers() -> None:
    # The system prompt tells the model to batch aggressively. A turn that batches a speculative
    # navigate that 404s WITH a fallback navigate to a live page must NOT be terminated on the first
    # of the batch — the fallback runs and the run proceeds to the model's own verdict.
    live = _navigate_tool(dead_end_status=None)
    dead = _navigate_tool(dead_end_status=404)
    tools = [
        ToolSpec(name="navigate_dead", description="d", parameters=dead.parameters, handler=dead.handler),
        ToolSpec(name="navigate_live", description="l", parameters=live.parameters, handler=live.handler),
        make_finish_tool(),
    ]
    script = [
        [("navigate_dead", {"url": "https://jobs.example.test/acme/closed"}), ("navigate_live", {"url": "x"})],
        [("finish", {"status": "completed", "reason": "applied to the live one"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert outcome.reason == "applied to the live one"


@pytest.mark.parametrize("status", sorted(NAVIGATION_DEAD_END_STATUSES))
@pytest.mark.asyncio
async def test_initial_navigation_dead_end_terminates_before_loop(status: int) -> None:
    # The dominant dead-posting case: the task's STARTING url is dead. It is navigated during browser
    # setup (before this loop), so the model never calls the `navigate` tool — it just observes the dead
    # page and finishes (defaulting to failed). The loop must classify the pre-loop status and end
    # `terminated` WITHOUT ever calling the model.
    outcome, caller = await _run(
        [[("finish", {"status": "completed", "reason": "should never run"})]],
        [make_finish_tool()],
        initial_navigation_status=status,
    )

    assert outcome.status == "terminated"
    assert outcome.reason.startswith(NAV_DEAD_END_REASON_PREFIX)
    assert caller.calls == 0  # short-circuited before the first LLM call — deterministic, not model-driven
    assert outcome.turns == 0


@pytest.mark.parametrize("status", [None, 200, 302, 401, 403, 429, 500, 503])
@pytest.mark.asyncio
async def test_initial_navigation_non_dead_end_runs_normally(status: int | None) -> None:
    # Anti-over-route: a reachable/soft/recoverable starting status must NOT short-circuit — the run
    # proceeds and finishes on the model's own verdict.
    outcome, caller = await _run(
        [[("finish", {"status": "completed", "reason": "applied"})]],
        [make_finish_tool()],
        initial_navigation_status=status,
    )

    assert outcome.status == "completed"
    assert outcome.reason == "applied"
    assert caller.calls == 1


@pytest.mark.asyncio
async def test_initial_navigation_dead_end_yields_canceled_when_cancelling() -> None:
    # A run canceled during setup must persist as `canceled` (and stay unbilled), not be pre-empted
    # into `terminated` by the pre-loop dead-end fast path — cancellation is checked first, exactly as
    # the first loop turn would.
    async def _cancel() -> bool:
        return True

    outcome, caller = await _run(
        [[("finish", {"status": "completed", "reason": "x"})]],
        [make_finish_tool()],
        initial_navigation_status=404,
        should_cancel=_cancel,
    )

    assert outcome.status == "canceled"
    assert caller.calls == 0


@pytest.mark.asyncio
async def test_verification_blocker_refuses_completed_but_not_failed() -> None:
    async def _blocked() -> str | None:
        return "verification never delivered a code"

    tools = [make_finish_tool(verification_blocker=_blocked)]
    script = [
        [("finish", {"status": "completed", "reason": "done"})],
        [("finish", {"status": "failed", "reason": "verification never delivered a code"})],
    ]
    outcome, _ = await _run(script, tools)

    tool_messages = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "finish"]
    assert any("verification never delivered a code" in m["content"] for m in tool_messages)
    assert outcome.status == "failed"


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
    # Deliberate contract change: the deadline trip grants one final observed turn before the run
    # actually ends, so one LLM call now runs (the raw literal moved to cap_trip).
    assert "deadline" not in outcome.reason
    assert outcome.cap_trip == "deadline (60s) reached"
    assert caller.calls == 1


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
    # Deliberate contract change: the trip after turn 1 grants one more observed turn (turn 2) before
    # the run actually ends, so two turns now run instead of one.
    assert "max_tokens" not in outcome.reason
    assert outcome.cap_trip == "max_tokens (10) reached"
    assert caller.calls == 2  # the granted turn ran before the run actually ended


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
        llm_caller=_ScriptedCaller(
            [
                [("observe", {}), ("observe", {})],  # two tool calls in one turn
                # Deliberate contract change: the mid-batch trip grants one final observed turn
                # (unconstrained -- the cap that stopped the batch above is not re-enforced here).
                [("observe", {})],
            ]
        ),
        system_prompt="sys",
        user_prompt="goal",
        tools=tools,
        max_turns=20,
        max_tool_calls=1,  # only one dispatch allowed
    )
    assert outcome.status == "budget_exhausted"
    assert outcome.cap_trip == "max_tool_calls (1) reached"
    assert len(observe_calls) == 2  # 1 from the capped batch + 1 from the granted final turn


@pytest.mark.asyncio
async def test_budget_trip_grants_one_final_observed_turn() -> None:
    # The core of the grant: a run that trips its cap mid-extraction gets exactly one more turn, the
    # trip is announced as a typed observation, and a finish on that turn carries its output out.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", click_calls), make_finish_tool()]
    script = [
        [("click", {})],
        [("click", {})],
        [("finish", {"status": "completed", "reason": "got what was needed", "extracted_output": {"partial": 1}})],
    ]
    outcome, caller = await _run(script, tools, max_turns=2)

    assert outcome.status == "completed"
    assert outcome.reason == "got what was needed"
    assert outcome.extracted_output == {"partial": 1}
    assert outcome.cap_trip == "max_turns (2) reached"
    assert caller.calls == 3  # the 2 budgeted turns plus exactly one granted final turn
    budget_msgs = [
        m
        for m in caller.message_history
        if m.get("role") == "user" and '"budget_exhausted":true' in str(m.get("content"))
    ]
    assert len(budget_msgs) == 1
    assert '"cap":"max_turns (2) reached"' in budget_msgs[0]["content"]
    assert '"tool_calls"' in budget_msgs[0]["content"] and '"tokens"' in budget_msgs[0]["content"]


@pytest.mark.asyncio
async def test_final_turn_without_finish_exits_honestly() -> None:
    # Single-shot: a granted turn that observes instead of finishing ends the run — no second
    # observation, no second grant, and the reason is a sentence while the raw cap rides cap_trip.
    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), make_finish_tool()]
    script = [[("observe", {})], [("observe", {})], [("observe", {})]]
    outcome, caller = await _run(script, tools, max_turns=2)

    assert outcome.status == "budget_exhausted"
    assert outcome.cap_trip == "max_turns (2) reached"
    assert "max_turns (" not in outcome.reason
    assert caller.calls == 3  # exactly one bonus turn
    budget_msgs = [
        m
        for m in caller.message_history
        if m.get("role") == "user" and '"budget_exhausted":true' in str(m.get("content"))
    ]
    assert len(budget_msgs) == 1


@pytest.mark.asyncio
async def test_token_reserve_trips_early_and_funds_the_final_turn() -> None:
    # The scripted caller reports 15 tokens/turn. Raw max_tokens=25 would allow a second unremarked
    # turn; the 15-token reserve trips the adjusted check after one turn (15 >= 25-15), so the second
    # turn is the granted final turn — proven by cap_trip being set at all.
    tools = [_recording_tool("observe", []), make_finish_tool()]
    script = [
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "ok", "extracted_output": {"k": 2}})],
    ]
    outcome, caller = await _run(script, tools, max_tokens=25, final_turn_token_reserve=15)

    assert outcome.status == "completed"
    assert outcome.cap_trip == "max_tokens (25) reached"
    assert outcome.extracted_output == {"k": 2}
    assert caller.calls == 2


@pytest.mark.asyncio
async def test_midbatch_tool_call_trip_answers_skips_then_grants_final_turn() -> None:
    # A mid-batch trip must leave a valid transcript: the undispatched calls get skipped answers
    # BEFORE the typed observation, and the granted turn's finish still carries its output out.
    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), make_finish_tool()]
    script = [
        [("observe", {}), ("observe", {}), ("observe", {})],
        [("finish", {"status": "completed", "reason": "ok", "extracted_output": {"n": 3}})],
    ]
    outcome, caller = await _run(script, tools, max_tool_calls=1)

    assert outcome.status == "completed"
    assert outcome.cap_trip == "max_tool_calls (1) reached"
    assert outcome.extracted_output == {"n": 3}
    assert len(observe_calls) == 1  # the cap stopped the batch after the first dispatch
    history = caller.message_history
    skipped_idx = [i for i, m in enumerate(history) if m.get("role") == "tool" and "skipped:" in str(m.get("content"))]
    budget_idx = [
        i
        for i, m in enumerate(history)
        if m.get("role") == "user" and '"budget_exhausted":true' in str(m.get("content"))
    ]
    assert len(skipped_idx) == 2 and len(budget_idx) == 1
    assert max(skipped_idx) < budget_idx[0]


@pytest.mark.asyncio
async def test_step_cap_trip_after_refused_extension_grants_final_turn() -> None:
    # The step gate (post-extension-refusal) is a grant site like every other cap, and a finish on
    # the granted turn keeps the model's own verdict — a failed status with its reason verbatim.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", click_calls)
    click.billable = True
    script = [
        [("click", {})],
        [("click", {})],
        [("finish", {"status": "failed", "reason": "blocked by a captcha", "extracted_output": {"rows": [1]}})],
    ]
    outcome, _ = await _run(script, [click, make_finish_tool()], max_action_steps=1, max_turns=20)

    assert outcome.status == "failed"
    assert outcome.reason == "blocked by a captcha"
    assert outcome.extracted_output == {"rows": [1]}
    assert outcome.cap_trip == "Reached the maximum steps (1)"
    assert len(click_calls) == 1  # round 2 was blocked at the gate; the granted turn chose to finish


@pytest.mark.asyncio
async def test_spent_grant_caught_at_the_step_gate_reports_the_granting_cap() -> None:
    # Cross-axis: max_turns granted the final turn, whose billable dispatch then hits the step
    # gate. The cap that granted the turn is the honest fact — not the gate that caught it.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", click_calls)
    click.billable = True
    script = [[("click", {})], [("click", {})]]
    outcome, _ = await _run(script, [click, make_finish_tool()], max_turns=1, max_action_steps=1)

    assert outcome.status == "budget_exhausted"
    assert outcome.cap_trip == "max_turns (1) reached"
    assert "turn budget" in outcome.reason


@pytest.mark.asyncio
async def test_step_gate_on_the_granted_turn_salvages_a_staged_finish_output() -> None:
    # The granted turn batches an over-cap action AND a finish: the refused action voids the
    # verdict (its premise never ran, so a completed claim there could be a success that never
    # happened) but the extraction the finish already carried must not be re-discarded.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", click_calls)
    click.billable = True
    script = [
        [("click", {})],
        [("click", {})],
        [("click", {}), ("finish", {"status": "completed", "reason": "done", "extracted_output": {"rows": [7]}})],
    ]
    outcome, _ = await _run(script, [click, make_finish_tool()], max_action_steps=1, max_turns=20)

    assert outcome.status == "budget_exhausted"
    assert outcome.extracted_output == {"rows": [7]}
    assert outcome.cap_trip == "Reached the maximum steps (1)"
    assert len(click_calls) == 1


@pytest.mark.asyncio
async def test_failure_skipped_finish_on_the_granted_turn_still_carries_its_extraction() -> None:
    # The granted turn batches an erroring action with a finish queued behind it: the failure skip
    # voids the verdict (written before the model saw the error), but the extraction it staged
    # rides the spent-grant exit instead of being re-discarded.
    async def err_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.error("boom")

    err_click = ToolSpec(
        name="click",
        description="click",
        parameters={"type": "object", "properties": {}},
        handler=err_handler,
        billable=True,
    )
    script = [
        [("observe", {})],
        [("click", {}), ("finish", {"status": "completed", "reason": "done", "extracted_output": {"rows": [9]}})],
    ]
    outcome, _ = await _run(script, [err_click, _recording_tool("observe", []), make_finish_tool()], max_turns=1)

    assert outcome.status == "budget_exhausted"
    assert outcome.extracted_output == {"rows": [9]}
    assert outcome.cap_trip == "max_turns (1) reached"


@pytest.mark.asyncio
async def test_guard_terminal_on_the_granted_turn_carries_the_cap_and_staged_extraction() -> None:
    # A terminal the loop itself creates on the granted turn (the action-loop terminator here,
    # skipping the finish batched behind the repeated click) still happened under the cap grant:
    # it must carry cap_trip and the staged extraction, or the consumer never sees the data.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), make_finish_tool()]
    script = [
        [("click", {"selector": "#submit"})],
        [
            ("click", {"selector": "#submit"}),
            ("finish", {"status": "completed", "reason": "done", "extracted_output": {"rows": [3]}}),
        ],
    ]
    outcome, _ = await _run(script, tools, max_turns=1, action_nudge_after=None, action_terminate_after=2)

    assert outcome.status == "terminated"
    assert outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)
    assert outcome.cap_trip == "max_turns (1) reached"
    assert outcome.extracted_output == {"rows": [3]}


@pytest.mark.asyncio
async def test_finish_staged_in_the_cap_granting_batch_is_salvaged_if_not_restated() -> None:
    # The batch that TRIPS the cap can itself stage a finish behind the over-cap call; when the
    # granted turn doesn't restate it, that extraction still rides the spent-grant exit.
    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), make_finish_tool()]
    script = [
        [
            ("observe", {}),
            ("observe", {}),
            ("finish", {"status": "completed", "reason": "done", "extracted_output": {"rows": [5]}}),
        ],
        [("observe", {})],
    ]
    outcome, _ = await _run(script, tools, max_tool_calls=1)

    assert outcome.status == "budget_exhausted"
    assert outcome.cap_trip == "max_tool_calls (1) reached"
    assert outcome.extracted_output == {"rows": [5]}


@pytest.mark.asyncio
async def test_completed_final_turn_without_restated_output_is_filled_from_the_staged_finish() -> None:
    # The granted turn's finish(completed) needn't re-type the extraction its skipped attempt
    # already staged: the missing output is filled from it, or an otherwise successful extraction
    # run gets demoted for missing extraction downstream.
    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), make_finish_tool()]
    script = [
        [
            ("observe", {}),
            ("observe", {}),
            ("finish", {"status": "completed", "reason": "done", "extracted_output": {"rows": [5]}}),
        ],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(script, tools, max_tool_calls=1)

    assert outcome.status == "completed"
    assert outcome.cap_trip == "max_tool_calls (1) reached"
    assert outcome.extracted_output == {"rows": [5]}


@pytest.mark.asyncio
async def test_loop_error_on_the_granted_call_still_carries_the_staged_extraction() -> None:
    # The granted LLM call itself failing (provider error) must not re-discard what the granting
    # batch staged: the loop_error keeps its own reason, but cap_trip and the extraction ride out.
    class _FailsSecondCallCaller(_ScriptedCaller):
        async def call(self, **kwargs: Any) -> dict[str, Any]:
            if self.calls >= 1:
                self.calls += 1
                raise RuntimeError("provider unavailable")
            return await super().call(**kwargs)

    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), make_finish_tool()]
    caller = _FailsSecondCallCaller(
        [
            [
                ("observe", {}),
                ("observe", {}),
                ("finish", {"status": "completed", "reason": "done", "extracted_output": {"rows": [8]}}),
            ]
        ]
    )
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=tools,
        max_turns=20,
        max_tool_calls=1,
    )

    assert outcome.status == "loop_error"
    assert "llm_call_failed" in outcome.reason
    assert outcome.cap_trip == "max_tool_calls (1) reached"
    assert outcome.extracted_output == {"rows": [8]}


@pytest.mark.asyncio
async def test_finish_on_the_granted_turn_is_not_held_for_settling() -> None:
    # The settle hold's retry turn no longer exists on the granted final turn: holding there would
    # silently convert the model's verdict into budget_exhausted — the verdict must stand instead.
    fp = {"n": 0}

    async def page_fingerprint() -> str:
        fp["n"] += 1
        return f"fp-{fp['n']}"  # never settles: every sample differs

    activity = ActivityRecency()
    finish = make_finish_tool(page_fingerprint=page_fingerprint, activity=activity, settle_wait_seconds=0.0)
    observe_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("observe", observe_calls), finish]
    script = [
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "done", "extracted_output": {"k": 1}})],
    ]
    outcome, _ = await _run(script, tools, max_turns=1, activity=activity)

    assert outcome.status == "completed"
    assert outcome.extracted_output == {"k": 1}
    assert outcome.cap_trip == "max_turns (1) reached"


@pytest.mark.asyncio
async def test_tool_error_stops_batch_and_skips_remaining() -> None:
    # A failed call whose own result signals a page transition must still stop the rest of the batch
    # (so a later write can't run against a page the failed call left in a different state) and
    # answer the skipped calls, so the next turn sees a valid transcript and re-plans from the error.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    boom_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _recording_tool("click", click_calls),
        _erroring_tool("boom", boom_calls, error_data={"page_transitioned": True}),
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
    assert any(m.get("name") == "boom" and "boom failed" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_non_mutating_tool_error_lets_independent_batch_calls_run() -> None:
    # A `type` failure that leaves the page unchanged (no page-transition data, probe reads
    # unchanged) must not block unrelated select_option calls later in the same batch -- only a
    # failure that may have left the page in an unplanned-for state should stop the batch. `type`
    # and `select_option` are marked billable=True to match production (tools.py) so this exercises
    # the real probe-gated branch, not a fixture shortcut that skips it.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    select_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return "doc-1"

    tools = [
        _recording_tool("click", click_calls),
        _recording_tool("type", type_calls, raises=True, billable=True),
        _recording_tool("select_option", select_calls, billable=True),
        make_finish_tool(),
    ]
    script = [
        [
            ("click", {"selector": "#ok"}),
            ("type", {"selector": "#name"}),
            ("select_option", {"selector": "#a"}),
            ("select_option", {"selector": "#b"}),
            ("select_option", {"selector": "#c"}),
        ],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "completed"
    assert len(select_calls) == 3  # all three ran despite the earlier, non-page-mutating error
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "type" and "tool_error" in m["content"] for m in turn1_tool_msgs)
    assert not any("skipped" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_failed_call_skips_only_same_selector_dependents() -> None:
    # A failed `type` on "#q" should skip a later call that targets the SAME selector (it depends on
    # the failed call having succeeded) but must not skip a call against an unrelated selector. All
    # three tools are billable=True to match production (tools.py), with a constant probe so the
    # probe-gated branch runs and reads unchanged.
    type_calls: list[tuple[str, dict[str, Any]]] = []
    press_calls: list[tuple[str, dict[str, Any]]] = []
    select_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return "doc-1"

    tools = [
        _recording_tool("type", type_calls, raises=True, billable=True),
        _recording_tool("press_key", press_calls, billable=True),
        _recording_tool("select_option", select_calls, billable=True),
        make_finish_tool(),
    ]
    script = [
        [
            ("type", {"selector": "#q"}),
            ("press_key", {"selector": "#q"}),
            ("select_option", {"selector": "#z"}),
        ],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "completed"
    assert len(press_calls) == 0  # depends on the failed #q type, must not run
    assert len(select_calls) == 1  # unrelated selector, must run
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(
        m.get("name") == "press_key" and "skipped" in m["content"] and "#q" in m["content"] for m in turn1_tool_msgs
    )


@pytest.mark.asyncio
async def test_failed_call_skips_only_same_mark_dependents() -> None:
    # A failed `type(mark=3)` should skip a later call on the SAME mark, mirroring the selector case,
    # since act-by-mark calls carry no top-level "selector" arg for _call_selector to key on. The
    # dependents are selects, not clicks: a click after any batch failure is deferred as a possible submit.
    type_calls: list[tuple[str, dict[str, Any]]] = []
    select_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _recording_tool("type", type_calls, raises=True, billable=True),
        _recording_tool("select_option", select_calls),
        make_finish_tool(),
    ]
    script = [
        [
            ("type", {"mark": 3}),
            ("select_option", {"mark": 3}),
            ("select_option", {"mark": 4}),
        ],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert len(select_calls) == 1  # only mark=4 dispatched; mark=3 depends on the failed type
    assert select_calls[0][1]["mark"] == 4
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "select_option" and "skipped" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_mid_batch_look_defers_every_later_mark_call() -> None:
    # look() renumbers marks on every call, so a mark=3 queued behind a mid-batch look was chosen from
    # the OLD screenshot and now names an arbitrary element: it is deferred, not dispatched.
    type_calls: list[tuple[str, dict[str, Any]]] = []
    look_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return "doc-1"

    async def type_handler(args: dict[str, Any]) -> ToolResult:
        type_calls.append(("type", args))
        if len(type_calls) == 1:
            return ToolResult.error("type failed")
        return ToolResult.ok("type done")

    tools = [
        ToolSpec(
            name="type",
            description="type",
            parameters={"type": "object", "properties": {}},
            handler=type_handler,
            billable=True,
        ),
        _look_tool(look_calls),
        make_finish_tool(),
    ]
    script = [
        [
            ("type", {"mark": 3}),
            ("look", {}),
            ("type", {"mark": 3}),
        ],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "completed"
    assert len(look_calls) == 1
    assert len(type_calls) == 1  # the second mark=3 is deferred: its number predates the renumbering
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "type"]
    assert any("renumbered" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_non_page_action_failure_does_not_mark_its_selector_or_arm_the_batch() -> None:
    # A timed-out wait on #x mutates nothing: the later click on #x re-resolves the element itself, so
    # it must dispatch rather than be skipped as a dependent, and no submit deferral is armed.
    wait_calls: list[tuple[str, dict[str, Any]]] = []
    click_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return "doc-1"

    async def wait_handler(args: dict[str, Any]) -> ToolResult:
        wait_calls.append(("wait", args))
        return ToolResult.error("wait timed out")

    tools = [
        ToolSpec(
            name="wait", description="wait", parameters={"type": "object", "properties": {}}, handler=wait_handler
        ),
        _recording_tool("click", click_calls),
        make_finish_tool(),
    ]
    script = [
        [("wait", {"selector": "#x"}), ("click", {"selector": "#x"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "completed"
    assert len(wait_calls) == 1
    assert len(click_calls) == 1


@pytest.mark.asyncio
async def test_non_billable_failure_that_moved_the_page_still_stops_the_batch() -> None:
    # A wait that times out BECAUSE the site navigated is not a field failure, but the page moved: the
    # probe runs around every known tool, so the rest of the batch (planned for the old page) is skipped.
    readings = iter(["doc-1", "doc-2"])
    click_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return next(readings, "doc-2")

    async def wait_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.error("wait timed out")

    tools = [
        ToolSpec(
            name="wait", description="wait", parameters={"type": "object", "properties": {}}, handler=wait_handler
        ),
        _recording_tool("click", click_calls),
        make_finish_tool(),
    ]
    script = [
        [("wait", {"selector": "#x"}), ("click", {"selector": "#next"})],
        [("finish", {"status": "terminated", "reason": "gave up"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "terminated"
    assert click_calls == []
    click_msgs = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "click"]
    assert any("changed the page" in m["content"] for m in click_msgs)


@pytest.mark.asyncio
async def test_hung_page_probe_is_bounded_and_reads_as_poisoned(monkeypatch: pytest.MonkeyPatch) -> None:
    # A renderer that never answers the probe must not stall the loop past its deadline: the sample is
    # bounded, and a missing reading is treated as poisoned (the batch stops), never as unchanged.
    monkeypatch.setattr(loop_module, "_PAGE_PROBE_TIMEOUT_SECONDS", 0.01)
    type_calls: list[tuple[str, dict[str, Any]]] = []

    async def hung_probe() -> str | None:
        await asyncio.Event().wait()
        return None

    async def type_handler(args: dict[str, Any]) -> ToolResult:
        type_calls.append(("type", args))
        if args.get("selector") == "#q":
            return ToolResult.error("type failed")
        return ToolResult.ok("type done")

    tools = [
        ToolSpec(
            name="type",
            description="type",
            parameters={"type": "object", "properties": {}},
            handler=type_handler,
            billable=True,
        ),
        make_finish_tool(),
    ]
    script = [
        [("type", {"selector": "#q"}), ("type", {"selector": "#zip"})],
        [("finish", {"status": "terminated", "reason": "gave up"})],
    ]
    outcome, _ = await asyncio.wait_for(_run(script, tools, page_probe=hung_probe), timeout=2)

    assert outcome.status == "terminated"
    assert [call_args.get("selector") for _, call_args in type_calls] == ["#q"]  # batch stopped: reading missing


@pytest.mark.asyncio
async def test_look_that_fails_before_renumbering_keeps_mark_dependents() -> None:
    # A look() refused on budget (or failing to capture/enumerate) leaves the old manifest live, so a
    # mark=3 call after it still names the element whose earlier call failed and must stay skipped.
    type_calls: list[tuple[str, dict[str, Any]]] = []
    look_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return "doc-1"

    async def type_handler(args: dict[str, Any]) -> ToolResult:
        type_calls.append(("type", args))
        return ToolResult.error("type failed")

    tools = [
        ToolSpec(
            name="type",
            description="type",
            parameters={"type": "object", "properties": {}},
            handler=type_handler,
            billable=True,
        ),
        _look_tool(look_calls, fail_before_renumbering=True),
        make_finish_tool(),
    ]
    script = [
        [
            ("type", {"mark": 3}),
            ("look", {}),
            ("type", {"mark": 3}),
        ],
        [("finish", {"status": "terminated", "reason": "gave up"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "terminated"
    assert len(look_calls) == 1
    assert len(type_calls) == 1  # the second mark=3 call is still a dependent of the failed one
    type_msgs = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "type"]
    assert any("skipped" in m["content"] for m in type_msgs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_status", "expected_turns", "expected_status"),
    [
        ("completed", 2, "completed"),
        ("terminated", 2, "terminated"),
        ("failed", 2, "failed"),
    ],
)
async def test_any_finish_deferred_after_batch_failure(
    finish_status: str, expected_turns: int, expected_status: str
) -> None:
    type_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return "doc-1"

    async def type_handler(args: dict[str, Any]) -> ToolResult:
        type_calls.append(("type", args))
        return ToolResult.error("type failed")

    tools = [
        ToolSpec(
            name="type",
            description="type",
            parameters={"type": "object", "properties": {}},
            handler=type_handler,
            billable=True,
        ),
        make_finish_tool(),
    ]
    script = [
        [("type", {"selector": "#q"}), ("finish", {"status": finish_status, "reason": "done"})],
        [("finish", {"status": finish_status, "reason": "done after re-checking"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    # Every verdict was written before the model saw the failure, so each is deferred one turn: a
    # completed one may be false, and a failed/terminated one carries a reason that predates the error.
    assert outcome.status == expected_status
    assert outcome.turns == expected_turns
    finish_msgs = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "finish"]
    assert any("skipped" in m["content"] for m in finish_msgs)


@pytest.mark.asyncio
async def test_cross_selector_dependent_call_still_dispatches_and_fails_on_its_own() -> None:
    # A call against a DIFFERENT selector than the failed one is not skipped by the same-selector
    # rule -- it dispatches and, if it truly depends on the failed call's DOM effect, fails on its
    # own terms rather than being wrong-committed as "skipped".
    calls: list[tuple[str, dict[str, Any]]] = []

    async def handler(args: dict[str, Any]) -> ToolResult:
        calls.append(("select_option", args))
        return ToolResult.error(f"no element for selector {args['selector']!r}")

    tools = [
        ToolSpec(
            name="select_option", description="s", parameters={"type": "object", "properties": {}}, handler=handler
        ),
        make_finish_tool(),
    ]
    script = [
        [("select_option", {"selector": "#a"}), ("select_option", {"selector": "#b"})],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert len(calls) == 2  # #b was dispatched -- its selector differs from #a's, so it is not skipped
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool" and m.get("name") == "select_option"]
    assert all("skipped" not in m["content"] for m in turn1_tool_msgs)
    assert any("no element for selector '#b'" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submit_call",
    [
        ("press_key", {"key": "Enter"}),
        ("press_key", {"key": "Control+Enter"}),
        ("press_key", {"selector": "#submit", "key": "Space"}),
        ("press_key", {"selector": "#submit", "key": " "}),
        ("type", {"selector": "#other", "press_enter": True}),
    ],
    ids=[
        "press_key_enter",
        "press_key_control_enter",
        "press_key_space",
        "press_key_literal_space",
        "type_press_enter",
    ],
)
async def test_click_and_enter_submit_skipped_after_batch_failure_but_other_fields_run(
    submit_call: tuple[str, dict[str, Any]],
) -> None:
    # After a page-action failure in the batch, the loop cannot classify a click -- it may be the
    # form's Submit -- so ANY later click is skipped alongside the Enter-shaped submit shapes. Other
    # field-filling tools (select_combobox, type, file_upload) on OTHER selectors are not submit-shaped
    # and still run.
    submit_name, submit_args = submit_call
    type_calls: list[tuple[str, dict[str, Any]]] = []
    click_calls: list[tuple[str, dict[str, Any]]] = []
    press_calls: list[tuple[str, dict[str, Any]]] = []
    combobox_calls: list[tuple[str, dict[str, Any]]] = []
    upload_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return "doc-1"

    async def type_handler(args: dict[str, Any]) -> ToolResult:
        type_calls.append(("type", args))
        if args.get("selector") == "#q":
            return ToolResult.error("type failed")
        return ToolResult.ok("type done")

    tools = [
        ToolSpec(
            name="type",
            description="type",
            parameters={"type": "object", "properties": {}},
            handler=type_handler,
            billable=True,
        ),
        _recording_tool("click", click_calls),
        _recording_tool("press_key", press_calls),
        _recording_tool("select_combobox", combobox_calls),
        _recording_tool("file_upload", upload_calls),
        make_finish_tool(),
    ]
    script = [
        [
            ("type", {"selector": "#q"}),
            ("click", {"selector": "#agree"}),
            (submit_name, submit_args),
            ("select_combobox", {"selector": "#city"}),
            ("type", {"selector": "#zip"}),
            ("file_upload", {"selector": "#resume"}),
        ],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "completed"
    assert len(click_calls) == 0  # a click cannot be classified as safe, so it's skipped too
    if submit_name == "press_key":
        assert len(press_calls) == 0  # Enter-shaped submit skipped after the batch failure
    else:
        # The submit-shaped `type` call is skipped before dispatch -- only the earlier, failed "#q"
        # call and the later "#zip" call reach the handler and land in the sink.
        assert not any(call_args.get("selector") == "#other" for _, call_args in type_calls)
    assert len(combobox_calls) == 1  # unrelated field, not submit-shaped, still runs
    assert any(call_args.get("selector") == "#zip" for _, call_args in type_calls)  # unrelated type still runs
    assert len(upload_calls) == 1  # unrelated field, not submit-shaped, still runs
    assert outcome.tool_calls == 5  # four dispatched calls plus finish: the two skipped calls cost no budget

    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "click" and "skipped" in m["content"] for m in turn1_tool_msgs)
    assert any(m.get("name") == submit_name and "skipped" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data_flag", "error_value", "expected_status"),
    [
        ("page_transitioned", True, "completed"),
        ("page_state_changed", True, "completed"),
        # navigation_dead_end additionally classifies the run as terminated once the batch settles --
        # that's a separate mechanism from the batch-stop this test targets, so it gets its own expected
        # final status rather than "completed".
        ("navigation_dead_end", 404, "terminated"),
    ],
)
async def test_page_changing_tool_error_still_stops_batch(
    data_flag: str, error_value: Any, expected_status: str
) -> None:
    # An error result that itself signals the page moved (page_transitioned, page_state_changed, or
    # navigation_dead_end in .data) must still stop the rest of the batch even though the call
    # "failed" -- the page moved out from under any planned follow-up regardless of the reported status.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    nav_click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _recording_tool("click", click_calls),
        _erroring_tool("nav_click", nav_click_calls, error_data={data_flag: error_value}),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script = [
        [
            ("click", {"selector": "#ok"}),
            ("nav_click", {"selector": "#nav"}),
            ("type", {"selector": "#x"}),
        ],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == expected_status
    assert len(type_calls) == 0  # the batch stopped: the page moved under the failed call
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "type" and "skipped" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_navigate_tool_error_still_stops_batch() -> None:
    # A failed `navigate` carries no explicit page_transitioned/page_state_changed data, but
    # navigation is inherently page-mutating -- an errored navigate must still stop the batch.
    navigate_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _recording_tool("navigate", navigate_calls, raises=True),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script = [
        [("navigate", {"url": "https://example.com"}), ("type", {"selector": "#x"})],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert len(type_calls) == 0
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "type" and "skipped" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_page_unavailable_tool_error_still_stops_batch() -> None:
    # The page itself is gone: inherently poisoning regardless of tool name or data.
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.error(PAGE_UNAVAILABLE_ERROR)

    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        ToolSpec(name="click", description="c", parameters={"type": "object", "properties": {}}, handler=handler),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script = [
        [("click", {"selector": "#ok"}), ("type", {"selector": "#x"})],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools)

    assert outcome.status == "completed"
    assert len(type_calls) == 0
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "type" and "skipped" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_page_probe_change_across_failed_call_stops_batch() -> None:
    # A billable tool's error carries no data flag, but the page_probe sampled before and after the
    # dispatch shows the page changed underneath it -- still poisoning.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    probe_calls = {"n": 0}

    async def probe() -> str | None:
        probe_calls["n"] += 1
        return "A" if probe_calls["n"] == 1 else "B"

    tools = [
        _erroring_tool("click", click_calls, billable=True),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script = [
        [("click", {"selector": "#ok"}), ("type", {"selector": "#x"})],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "completed"
    assert len(type_calls) == 0  # the probe changed across the failed call -- batch stopped
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "type" and "skipped" in m["content"] for m in turn1_tool_msgs)


@pytest.mark.asyncio
async def test_page_probe_unchanged_across_failed_call_continues_batch() -> None:
    # Same shape as above, but the probe reads the same value before and after the failed call --
    # nothing in this error signals a page change, so the batch continues.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []

    async def probe() -> str | None:
        return "same"

    tools = [
        _erroring_tool("click", click_calls, billable=True),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script = [
        [("click", {"selector": "#ok"}), ("type", {"selector": "#x"})],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "completed"
    assert len(type_calls) == 1  # the probe read unchanged across the failed call -- batch continues


@pytest.mark.asyncio
async def test_recordable_non_billable_tool_error_with_probe_change_stops_batch() -> None:
    # solve_captcha is recordable but not billable -- the probe must still be sampled around it, or a
    # failed solve that moved the page can never poison the batch.
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    probe_calls = {"n": 0}

    async def probe() -> str | None:
        probe_calls["n"] += 1
        return "doc-1" if probe_calls["n"] == 1 else "doc-2"

    tools = [
        _erroring_tool("solve_captcha", click_calls, recordable=True),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script = [
        [("solve_captcha", {}), ("type", {"selector": "#x"})],
        [("finish", {"status": "completed", "reason": "recovered"})],
    ]
    outcome, _ = await _run(script, tools, page_probe=probe)

    assert outcome.status == "completed"
    assert len(type_calls) == 0  # the probe changed across the failed recordable call -- batch stopped
    turn1_tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m.get("name") == "type" and "skipped" in m["content"] for m in turn1_tool_msgs)


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
        # Deliberate contract change: the block above grants one final observed turn; retrying the
        # same over-cap round on it hits the gate again and ends the run for real.
        [("click", {}), ("type", {"t": "d"})],
    ]
    outcome, _ = await _run(script, [click, type_, make_finish_tool()], max_action_steps=2, max_turns=20)
    assert outcome.status == "budget_exhausted"
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    assert outcome.action_steps == 2  # two action rounds counted, exposed on the outcome
    # 2 rounds (4 actions) ran; rounds 3 and 4 were both blocked at the top -> per-round counting.
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
    # 2nd and 3rd clicks are both beyond-cap attempts; the 3rd is the deliberate contract change:
    # the 2nd blocked attempt grants one final observed turn, and retrying on it ends the run for real.
    script = [[("click", {})], [("click", {})], [("click", {})]]
    outcome, _ = await _run(script, [click, make_finish_tool()], max_action_steps=1, max_turns=20)
    assert outcome.status == "budget_exhausted"
    assert "maximum steps (1)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (1)"
    assert outcome.action_steps == 1
    assert len(click_calls) == 1  # both over-budget actions were refused, not executed


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
async def test_action_step_budget_extends_once_for_a_progressing_run() -> None:
    # A run whose page keeps changing (a repeated probe returning fresh content) at the cap earns
    # ONE bounded extension instead of dying mid-progress on a genuinely long form.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", ["page 1", "page 2", "page 3"])
    script = [
        [("observe", {})],
        [("click", {"selector": "#a"})],  # round 1
        [("observe", {})],  # content changed -> progressed evidence
        [("click", {"selector": "#b"})],  # round 2 == cap
        [("observe", {})],  # fresh evidence again
        [("click", {"selector": "#c"})],  # beyond cap: progress-gated extension (2 -> 3)
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, [observe, click, make_finish_tool()], max_action_steps=2, max_turns=20)
    assert outcome.status == "completed"
    assert outcome.action_steps == 3
    assert len(clicks) == 3
    extended = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENDED_EVENT]
    assert len(extended) == 1 and extended[0]["extension"] == 1 and extended[0]["original_cap"] == 2


@pytest.mark.asyncio
async def test_action_step_budget_no_extension_without_page_change_evidence() -> None:
    # Absence of stall warnings is NOT progress: a run with no evidence the page ever changed is
    # refused at the original cap exactly as before, and the refusal is a queryable event.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    script = [
        [("click", {"selector": "#a"})],
        [("click", {"selector": "#b"})],
        [("click", {"selector": "#c"})],
        # Deliberate contract change: the #c block above grants one final observed turn; retrying
        # the same over-cap click on it hits the gate again (still no evidence) and ends the run.
        [("click", {"selector": "#c"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, [click, make_finish_tool()], max_action_steps=2, max_turns=20)
    assert outcome.status == "budget_exhausted"
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    assert len(clicks) == 2
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert len(refused) == 2 and all(r["gate_reason"] == "no_recent_page_change_evidence" for r in refused)


@pytest.mark.asyncio
async def test_action_step_budget_extension_repeats_but_is_bounded_at_a_multiple_of_the_original_cap() -> None:
    # A long form does not stop being long after one extension, so the grant repeats under the same
    # evidence predicate — but it is not unbounded. Each grant is half the ORIGINAL cap (linear, so
    # a run that has already spent 1.5x its budget draws the same size handout as one at first
    # exhaustion, never a compounding one), and growth stops dead at the limit multiple.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", [f"page {i}" for i in range(1, 20)])
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(14):
        script.append([("observe", {})])
        script.append([("click", {"selector": f"#a{i}"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [observe, click, make_finish_tool()],
            max_action_steps=4,
            max_turns=80,
            max_tool_calls=200,
        )
    assert outcome.status == "budget_exhausted"
    # 4 -> 6 -> 8 -> 10 -> 12, then the limit (4 * ACTION_BUDGET_EXTENSION_MAX_FACTOR) stops it.
    granted = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENDED_EVENT]
    assert [entry["original_cap"] for entry in granted] == [4, 6, 8, 10]
    assert {entry["extension"] for entry in granted} == {2}, "every grant is half the ORIGINAL cap"
    assert [entry["extensions_granted"] for entry in granted] == [1, 2, 3, 4]
    assert len(clicks) == 4 * ACTION_BUDGET_EXTENSION_MAX_FACTOR
    assert outcome.cap_trip == "Reached the maximum steps (12)"
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and all(entry["gate_reason"] == "extension_limit_reached" for entry in refused)


@pytest.mark.asyncio
async def test_action_step_budget_final_grant_truncates_to_the_limit_instead_of_overshooting() -> None:
    # The limit is enforced by clamping each grant to the headroom left under it, and on an EVEN base
    # cap that clamp never binds: half the cap divides the 2x headroom evenly, so the last full grant
    # lands exactly on the limit and a bare `extension = raw_extension` behaves identically. An odd
    # base cap is what separates them -- 25 leaves 2 steps under the limit after four 12s, and only
    # the clamp turns the fifth grant into that runt instead of overshooting the cap to 85.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", [f"page {i}" for i in range(1, 120)])
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(90):
        script.append([("observe", {})])
        script.append([("click", {"selector": f"#a{i}"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [observe, click, make_finish_tool()],
            max_action_steps=25,
            max_turns=400,
            max_tool_calls=800,
        )
    assert outcome.status == "budget_exhausted"
    granted = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENDED_EVENT]
    assert [entry["original_cap"] for entry in granted] == [25, 37, 49, 61, 73]
    assert [entry["extension"] for entry in granted] == [12, 12, 12, 12, 2], (
        "the final grant must truncate to the limit, not overshoot it by a full half-cap"
    )
    assert len(clicks) == 25 * ACTION_BUDGET_EXTENSION_MAX_FACTOR
    assert outcome.cap_trip == "Reached the maximum steps (75)"


@pytest.mark.asyncio
async def test_extension_publishes_the_moved_guards_to_the_live_activity_record() -> None:
    # The failure-evidence gates read ActivityRecency, and a grant moves the guards MID-BATCH. Two
    # writes on that path have no reader anywhere else in the suite because `_run` leaves
    # `activity=None`: the `*_remaining` refresh (gates later in this same batch would otherwise
    # judge the run on pre-grant headroom) and clearing `final_turn_active` on release (left stuck
    # True it refuses every finish hold for the rest of the run). Probes bracket the billable call
    # so one reads the record before the grant and one after, within a single batch.
    activity = ActivityRecency()
    snapshots: list[tuple[int, bool, int | None]] = []

    async def probe_handler(args: dict[str, Any]) -> ToolResult:
        snapshots.append((activity.turn, activity.final_turn_active, activity.tokens_remaining))
        return ToolResult.ok("probe")

    probe = ToolSpec(
        name="probe", description="probe", parameters={"type": "object", "properties": {}}, handler=probe_handler
    )
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", [f"page {i}" for i in range(1, 40)])
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(10):
        script.append([("observe", {})])
        script.append([("probe", {}), ("click", {"selector": f"#a{i}"}), ("probe", {})])

    with capture_logs() as logs:
        await _run(
            script,
            [observe, click, probe, make_finish_tool()],
            max_action_steps=4,
            max_turns=200,
            max_tool_calls=500,
            # Trips at the top of a turn well before the action cap, latching the wrap-up turn; the
            # grant during that turn re-derives the token guard to 1.5M and releases the latch.
            max_tokens=135,
            backstops_for_cap=taskv3_runaway_backstops,
            activity=activity,
        )
    released = [entry for entry in logs if entry["event"] == FINAL_TURN_RELEASED_EVENT]
    assert released, "the run never reached the release path this test exists to cover"
    release_turn = released[0]["turn"]
    # Only the mid-batch refresh makes the raised token guard visible to the rest of THIS turn;
    # without it the first roomy reading arrives at the top of the next one.
    roomy = [snapshot for snapshot in snapshots if snapshot[2] is not None and snapshot[2] > 135]
    assert roomy and roomy[0][0] == release_turn, (
        "the grant's raised guards were not published to the live record until the following turn"
    )
    assert any(snapshot[1] for snapshot in snapshots), "the wrap-up latch never engaged, so the reset is untested"
    latched_at = next(index for index, snapshot in enumerate(snapshots) if snapshot[1])
    assert any(not snapshot[1] for snapshot in snapshots[latched_at:]), (
        "final_turn_active stayed True after the release, which refuses every later finish hold"
    )


@pytest.mark.asyncio
async def test_action_step_budget_extension_refused_without_turn_headroom() -> None:
    # An extension the remaining turn budget cannot fund is refused — granting steps the runaway
    # guards would immediately revoke converts an honest exhaustion into a worse one.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", ["page 1", "page 2", "page 3"])
    script = [
        [("observe", {})],
        [("click", {"selector": "#a"})],
        [("observe", {})],
        [("click", {"selector": "#b"})],
        [("observe", {})],
        [("click", {"selector": "#c"})],
        [("click", {"selector": "#c"})],
    ]
    outcome, _ = await _run(
        script,
        [observe, click, make_finish_tool()],
        max_action_steps=2,
        max_turns=6,
        activity=ActivityRecency(),
    )
    assert outcome.status == "budget_exhausted"
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    assert len(clicks) == 2


@pytest.mark.asyncio
async def test_action_step_budget_extension_recovers_after_an_early_stall_window() -> None:
    # The no-net-progress veto reads CURRENT stalled-ness, not the progress ledger's one-shot
    # telemetry latch: a run that stalled early, then made sustained hard progress right up to the
    # cap, earns the extension.
    clicks: list[tuple[str, dict[str, Any]]] = []

    async def plain_handler(args: dict[str, Any]) -> ToolResult:
        clicks.append(("click", args))
        return ToolResult.ok("click done")

    async def transition_handler(args: dict[str, Any]) -> ToolResult:
        clicks.append(("click_transition", args))
        return ToolResult.ok("click done", data={"page_transitioned": True})

    observe_n = {"n": 0}

    async def observe_handler(args: dict[str, Any]) -> ToolResult:
        # Same content while stalling; fresh content once the run recovers, so evidence comes from
        # a content-confirmed progressed probe (URL-only transitions no longer stamp evidence).
        observe_n["n"] += 1
        content = "form page" if observe_n["n"] <= 2 else f"form page {observe_n['n']}"
        return ToolResult.ok(content, data={"summary": {"invalid_fields": 3}})

    click = ToolSpec(
        name="click",
        description="c",
        parameters={"type": "object", "properties": {}},
        handler=plain_handler,
        billable=True,
    )
    click_transition = ToolSpec(
        name="click_transition",
        description="c",
        parameters={"type": "object", "properties": {}},
        handler=transition_handler,
        billable=True,
    )
    observe = ToolSpec(
        name="observe",
        description="o",
        parameters={"type": "object", "properties": {}},
        handler=observe_handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],  # arms the progress ledger (invalid_fields=3)
        [("click", {"selector": f"#s{i}"}) for i in range(8)],  # one fruitless batch spanning the window
        [("observe", {})],  # flat confirm -> the ledger's shadow latch fires
    ]
    for i in range(3):  # sustained recovery: hard progress plus content-confirmed fresh observes
        script.append([("click_transition", {"selector": f"#p{i}"})])
        script.append([("observe", {})])  # changed content -> progressed probe stamps evidence
    script.append([("click", {"selector": "#final"})])  # beyond cap: extension must be granted
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script, [click, click_transition, observe, make_finish_tool()], max_action_steps=4, max_turns=30
        )
    assert [entry for entry in logs if entry["event"] == PROGRESS_LEDGER_SHADOW_EVENT]  # the latch DID fire
    assert outcome.status == "completed"
    assert [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENDED_EVENT]


@pytest.mark.asyncio
async def test_action_step_budget_extension_not_granted_on_pre_reload_evidence() -> None:
    # A reload re-baselines every ledger describing the old document, the evidence stamp included:
    # pre-reload progress says nothing about the fresh page, so the run must re-demonstrate
    # progress before it can earn an extension.
    reload_calls: list[None] = []

    async def reload_page() -> None:
        reload_calls.append(None)

    clicks: list[tuple[str, dict[str, Any]]] = []

    async def click_handler(args: dict[str, Any]) -> ToolResult:
        clicks.append(("click", args))
        if args.get("selector") == "#refresh-trigger":
            skyvern_context.current().refresh_working_page = True
        return ToolResult.ok("clicked")

    click = ToolSpec(
        name="click",
        description="c",
        parameters={"type": "object", "properties": {}},
        handler=click_handler,
        billable=True,
    )
    observe = _perception_tool("observe", ["page 1", "page 2"])
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],
        [("click", {"selector": "#a"})],
        [("observe", {})],  # progressed -> evidence
        [("click", {"selector": "#refresh-trigger"})],  # round 2 == cap; triggers a reload after
        [("click", {"selector": "#b"})],  # beyond cap, right after the reload: must be refused
    ]
    ctx = SkyvernContext(task_id="tsk_ext_reload")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs:
            outcome, _ = await _run(
                script, [click, observe, make_finish_tool()], max_action_steps=2, max_turns=20, reload_page=reload_page
            )
    finally:
        skyvern_context.reset()
    assert len(reload_calls) == 1
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "no_recent_page_change_evidence"


@pytest.mark.asyncio
async def test_action_step_budget_extension_respects_workflow_run_ceiling() -> None:
    # An org's workflow-run-wide step pool is a HARD ceiling the extension must never breach: when
    # the pool remainder supplied the effective cap, a progressing run is still refused.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", ["page 1", "page 2", "page 3"])
    script = [
        [("observe", {})],
        [("click", {"selector": "#a"})],
        [("observe", {})],
        [("click", {"selector": "#b"})],
        [("observe", {})],
        [("click", {"selector": "#c"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [observe, click, make_finish_tool()],
            max_action_steps=2,
            max_action_steps_ceiling=2,
            max_turns=20,
        )
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    assert len(clicks) == 2
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "hard_step_ceiling"


@pytest.mark.asyncio
async def test_action_step_budget_extension_truncated_to_workflow_run_ceiling() -> None:
    # A pool remainder above the cap but below cap+extension truncates the grant to what the pool
    # can fund, rather than refusing outright or breaching it.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", [f"page {i}" for i in range(1, 8)])
    script = [
        [("observe", {})],
        [("click", {"selector": "#a"})],
        [("observe", {})],
        [("click", {"selector": "#b"})],
        [("observe", {})],
        [("click", {"selector": "#c"})],
        [("observe", {})],
        [("click", {"selector": "#d"})],  # cap 4
        [("observe", {})],
        [("click", {"selector": "#e"})],  # extension would be 2; ceiling 5 truncates to 1
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [observe, click, make_finish_tool()],
            max_action_steps=4,
            max_action_steps_ceiling=5,
            max_turns=40,
        )
    assert outcome.status == "completed"
    assert outcome.action_steps == 5
    extended = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENDED_EVENT]
    assert extended and extended[0]["extension"] == 1


@pytest.mark.asyncio
async def test_action_step_budget_extension_not_laundered_by_same_url_reload() -> None:
    # A confirmed same-URL navigate reports page_state_changed (the retry ledger legitimately
    # resets) but flags same_url_reload: a reset is not progress, so it must CLEAR the extension
    # evidence exactly like the refresh-signal path, not stamp it.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)

    async def nav_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("navigated", data={"page_state_changed": True, "same_url_reload": True})

    navigate = ToolSpec(
        name="navigate", description="n", parameters={"type": "object", "properties": {}}, handler=nav_handler
    )
    script = [
        [("click", {"selector": "#a"})],
        [("click", {"selector": "#b"})],  # cap
        [("navigate", {"url": "https://example.test/apply"})],  # same-URL reload: not evidence
        [("click", {"selector": "#c"})],  # beyond cap: must be refused
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, [click, navigate, make_finish_tool()], max_action_steps=2, max_turns=20)
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    assert len(clicks) == 2
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "no_recent_page_change_evidence"


@pytest.mark.asyncio
async def test_action_step_budget_extension_deferred_while_a_refresh_is_pending() -> None:
    # A pending page-refresh signal voids the very action that would earn the grant and re-baselines
    # the page: the gate must not race it and spend the extension on pre-reload evidence.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", ["page 1", "page 2"])
    script = [
        [("observe", {})],
        [("click", {"selector": "#a"})],
        [("observe", {})],  # progressed -> evidence
        [("click", {"selector": "#b"})],  # cap reached
        [("click", {"selector": "#c"})],  # over cap; the refresh arrives DURING this model turn
    ]

    class _RefreshArmingCaller(_ScriptedCaller):
        async def call(self, **kwargs: Any) -> dict[str, Any]:
            if self.calls == 4:  # the turn whose tool call is the over-cap #c
                skyvern_context.current().refresh_working_page = True
            return await super().call(**kwargs)

    ctx = SkyvernContext(task_id="tsk_ext_refresh_race")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs:
            outcome = await run_agent_tool_loop(
                llm_caller=_RefreshArmingCaller(script),
                system_prompt="sys",
                user_prompt="goal",
                tools=[click, observe, make_finish_tool()],
                max_action_steps=2,
                max_turns=20,
                max_tool_calls=100,
            )
    finally:
        skyvern_context.reset()
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    assert len(clicks) == 2
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "refresh_pending"


@pytest.mark.asyncio
async def test_action_step_budget_extension_not_stamped_by_nav_revisit() -> None:
    # A hop back onto a recently-navigated URL (A->B->A) resets the retry ledger like any
    # navigation but is known territory — it must not stamp fresh-page extension evidence.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)

    async def nav_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("navigated", data={"page_state_changed": True, "nav_revisit": True})

    navigate = ToolSpec(
        name="navigate", description="n", parameters={"type": "object", "properties": {}}, handler=nav_handler
    )
    fresh_nav_calls: list[None] = []

    async def fresh_nav_handler(args: dict[str, Any]) -> ToolResult:
        fresh_nav_calls.append(None)
        return ToolResult.ok("navigated", data={"page_state_changed": True})

    fresh_navigate = ToolSpec(
        name="goto", description="n", parameters={"type": "object", "properties": {}}, handler=fresh_nav_handler
    )
    # The real two-hop shape: the A->B hop stamps genuine fresh-page evidence, then the B->A
    # revisit must CLEAR it — navigation is non-billable, so the action-round clock never advances
    # and a surviving stamp would stay maximally recent forever.
    script = [
        [("click", {"selector": "#a"})],
        [("click", {"selector": "#b"})],  # cap
        [("goto", {"url": "https://example.test/results"})],  # A->B: stamps evidence
        [("navigate", {"url": "https://example.test/apply"})],  # B->A revisit: clears it
        [("click", {"selector": "#c"})],  # beyond cap: refused
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script, [click, navigate, fresh_navigate, make_finish_tool()], max_action_steps=2, max_turns=20
        )
    assert len(fresh_nav_calls) == 1
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "no_recent_page_change_evidence"


@pytest.mark.asyncio
async def test_action_step_budget_extension_not_laundered_by_post_reload_observe() -> None:
    # A same-URL reload destroys the observed document: the perception ledgers must re-baseline
    # (as the refresh path does), or the first post-reload observe diffs against the PRE-reload
    # digest, reads as progressed, and stamps evidence without any progress on the fresh page.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", ["page 1", "page 2", "page 3 reloaded"])

    async def nav_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("navigated", data={"page_state_changed": True, "same_url_reload": True})

    navigate = ToolSpec(
        name="navigate", description="n", parameters={"type": "object", "properties": {}}, handler=nav_handler
    )
    script = [
        [("observe", {})],
        [("click", {"selector": "#a"})],
        [("observe", {})],  # progressed -> evidence
        [("click", {"selector": "#b"})],  # cap
        [("navigate", {"url": "https://example.test/apply"})],  # reload: clears stamp AND ledgers
        [("observe", {})],  # post-reload first look: no baseline, must NOT read as progressed
        [("click", {"selector": "#c"})],  # beyond cap: refused
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script, [click, navigate, observe, make_finish_tool()], max_action_steps=2, max_turns=20
        )
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    assert len(clicks) == 2
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "no_recent_page_change_evidence"


@pytest.mark.asyncio
async def test_action_step_budget_extension_not_stamped_by_url_only_transitions() -> None:
    # history.pushState moves the URL without changing the document: page_transitioned is a
    # URL-only hint, and a stalled run varying such clicks (evading the retry-streak veto) must
    # not launder evidence from it — content-confirmed signals are the evidence bar.
    clicks: list[tuple[str, dict[str, Any]]] = []

    async def push_state_click(args: dict[str, Any]) -> ToolResult:
        clicks.append(("click", args))
        return ToolResult.ok("clicked", data={"page_transitioned": True})

    click = ToolSpec(
        name="click",
        description="c",
        parameters={"type": "object", "properties": {}},
        handler=push_state_click,
        billable=True,
    )
    script = [
        [("click", {"selector": "#tab-1"})],
        [("click", {"selector": "#tab-2"})],  # cap; varied selectors keep the retry ledger cold
        [("click", {"selector": "#tab-3"})],  # beyond cap: URL-only hints are not evidence
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, [click, make_finish_tool()], max_action_steps=2, max_turns=20)
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    assert len(clicks) == 2
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "no_recent_page_change_evidence"


@pytest.mark.asyncio
async def test_action_step_budget_extension_granted_on_progressing_non_form_work() -> None:
    # The no-net-progress veto reads the ledger's own confirmed form-stall state: on a page with no
    # form, billable rounds still increment the raw counter, but a run demonstrating real progress
    # (changing probe content) must not be vetoed by a counter the ledger itself refuses to judge.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", [f"listing page {i}" for i in range(1, 12)])
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(8):  # window-many billable rounds on a form-less page, each with fresh content
        script.append([("observe", {})])
        script.append([("click", {"selector": f"#item-{i}"})])
    script.append([("observe", {})])
    script.append([("click", {"selector": "#next"})])  # beyond cap 8: extension must be granted
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, [observe, click, make_finish_tool()], max_action_steps=8, max_turns=40)
    assert outcome.status == "completed"
    assert outcome.action_steps == 9
    assert [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENDED_EVENT]


@pytest.mark.asyncio
async def test_action_step_budget_extension_survives_stale_perception_stall_flag() -> None:
    # perception_stall_imminent armed on the PREVIOUS document must not veto an extension after a
    # real page change invalidated that streak — positive page-change evidence clears the flag, as
    # the refresh path already does.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)

    async def nav_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("navigated", data={"page_state_changed": True})

    navigate = ToolSpec(
        name="navigate", description="n", parameters={"type": "object", "properties": {}}, handler=nav_handler
    )
    activity = ActivityRecency(perception_stall_imminent=True)
    script = [
        [("click", {"selector": "#a"})],
        [("click", {"selector": "#b"})],  # cap
        [("navigate", {"url": "https://example.test/step-2"})],  # fresh page: evidence + flag clear
        [("click", {"selector": "#c"})],  # beyond cap: granted
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(
        script, [click, navigate, make_finish_tool()], max_action_steps=2, max_turns=20, activity=activity
    )
    assert outcome.status == "completed"
    assert outcome.action_steps == 3
    assert activity.perception_stall_imminent is False


@pytest.mark.asyncio
async def test_action_step_budget_extension_dries_up_on_content_oscillation() -> None:
    # A page alternating between two known states (a panel toggling open and shut) is a cycle, not
    # progress: only genuinely NEW content stamps evidence, so the stamp from the first flip goes
    # stale and the oscillating run is refused at the cap.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    contents = ["panel closed", "panel open"]
    observe = _perception_tool("observe", [contents[i % 2] for i in range(24)])
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})]]
    for i in range(10):
        script.append([("click", {"selector": f"#toggle-{i}"})])  # varied: retry ledger stays cold
        script.append([("observe", {})])  # alternating known content
    script.append([("click", {"selector": "#over-cap"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, [observe, click, make_finish_tool()], max_action_steps=10, max_turns=60)
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (10)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (10)"
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "no_recent_page_change_evidence"


@pytest.mark.asyncio
async def test_action_step_budget_extension_not_stamped_by_replayed_download_notice() -> None:
    # A compactable tool replaying a retained download notice (download_notice without download_new)
    # re-clears the retry ledger but is not fresh progress: an old download must not keep the
    # evidence stamp maximally recent forever.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)

    async def replay_observe(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("page\nDownloaded: report.pdf (1.0 MB)", data={"download_notice": True})

    observe = ToolSpec(
        name="observe",
        description="o",
        parameters={"type": "object", "properties": {}},
        handler=replay_observe,
        compactable=True,
    )
    script = [
        [("click", {"selector": "#a"})],
        [("observe", {})],  # replayed notice: not evidence
        [("click", {"selector": "#b"})],  # cap
        [("observe", {})],  # replay again
        [("click", {"selector": "#c"})],  # beyond cap: refused
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, [click, observe, make_finish_tool()], max_action_steps=2, max_turns=20)
    assert outcome.status == "budget_exhausted"
    # Deliberate contract change: the refused-extension step-cap exit grants one final observed
    # turn first, and the raw cap literal lives on cap_trip while reason is a human sentence.
    assert "maximum steps (2)" not in outcome.reason
    assert outcome.cap_trip == "Reached the maximum steps (2)"
    refused = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENSION_REFUSED_EVENT]
    assert refused and refused[0]["gate_reason"] == "no_recent_page_change_evidence"


def test_content_only_perception_ignores_the_url_value() -> None:
    # The URL is a hint, not content: the evidence lane's digest ignores a history.pushState URL
    # flip (which would otherwise read as a progressed snapshot on a frozen document), while the
    # full canonicalization keeps the URL so wizard pages that differ only by URL still clear the
    # repeat guards.
    from skyvern.forge.taskv3.loop import _content_only_perception

    a = _content_only_perception("url=https://site.test/a title='T' (3 interactive elements)\nbutton#x")
    b = _content_only_perception("url=https://site.test/b title='T' (3 interactive elements)\nbutton#x")
    assert a == b
    c = _content_only_perception("url=https://site.test/a title='T' (4 interactive elements)\nbutton#y")
    assert a != c  # real content changes still differ
    full_a = _canonical_perception_content("url=https://site.test/a title='T' (3 interactive elements)\nbutton#x")
    full_b = _canonical_perception_content("url=https://site.test/b title='T' (3 interactive elements)\nbutton#x")
    assert full_a != full_b  # the guard-clearing digest still sees the URL


def test_budget_extension_gate_deadline_scales_with_observed_pace() -> None:
    # Funding the extension in wall-clock: a run that burned ~30s per step cannot run a 5-step
    # extension in 120s, even though the flat minimum headroom is met.
    now = time.monotonic()
    ok, _ = _budget_extension_gate(10, 9, set(), False, None, now + 1200, 5, seconds_per_step=30.0)
    assert ok
    assert _budget_extension_gate(10, 9, set(), False, None, now + 120, 5, seconds_per_step=30.0) == (
        False,
        "insufficient_deadline_headroom",
    )


@pytest.mark.asyncio
async def test_page_state_stall_nudges_then_terminates_a_frozen_page_cycle() -> None:
    # SKY-15265: a tool cycle that leaves the page fingerprint byte-identical round after round is
    # a stall no per-tool guard can see (varied selectors never streak; scroll/wait carry no
    # digest). The detector re-plans the model once, then ends the run with a facetable verdict.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", [f"panel variant {i}" for i in range(30)])

    async def frozen_fingerprint() -> str:
        return "FROZEN-DOM"

    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(14):
        script.append([("observe", {})])
        script.append([("click", {"selector": f"#try-{i}"})])  # varied: the action-loop guard is blind
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [observe, click, make_finish_tool()],
            page_fingerprint=frozen_fingerprint,
            max_action_steps=24,
            max_turns=60,
            max_tool_calls=200,
        )
    assert outcome.status == "completed"  # the verdict is SHADOW-only: measured, never enforced yet
    nudges = [m for m in outcome.messages if m.get("role") == "user" and "unchanged" in str(m.get("content"))]
    assert len(nudges) == 1  # exactly one re-plan nudge
    shadow = [entry for entry in logs if entry["event"] == PAGE_STATE_STALL_SHADOW_EVENT]
    assert len(shadow) == 1 and shadow[0]["rounds"] == 12
    assert len(clicks) == 14  # nothing was cut short


@pytest.mark.asyncio
async def test_page_state_stall_never_fires_while_the_fingerprint_moves() -> None:
    # A real form fill mutates innerHTML every round, so the fingerprint moves and the detector
    # stays silent for the life of the run.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    fp_n = {"n": 0}

    async def moving_fingerprint() -> str:
        fp_n["n"] += 1
        return f"dom-{fp_n['n']}"

    script: list[list[tuple[str, dict[str, Any]]]] = [[("click", {"selector": f"#field-{i}"})] for i in range(14)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(
        script,
        [click, make_finish_tool()],
        page_fingerprint=moving_fingerprint,
        max_action_steps=24,
        max_turns=60,
        max_tool_calls=200,
    )
    assert outcome.status == "completed"
    assert len(clicks) == 14


@pytest.mark.asyncio
async def test_page_state_stall_counter_resets_when_the_cycle_breaks_after_the_nudge() -> None:
    # The nudge is a real second chance: a run that changes the page after being warned survives.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    fp_state = {"n": 0}

    async def thawing_fingerprint() -> str:
        fp_state["n"] += 1
        # Two samples per round: frozen through round 9's after-sample (18 calls), moving after.
        return "FROZEN" if fp_state["n"] <= 18 else f"dom-{fp_state['n']}"

    script: list[list[tuple[str, dict[str, Any]]]] = [[("click", {"selector": f"#try-{i}"})] for i in range(12)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [click, make_finish_tool()],
            page_fingerprint=thawing_fingerprint,
            max_action_steps=24,
            max_turns=60,
            max_tool_calls=200,
        )
    assert outcome.status == "completed"
    assert len(clicks) == 12
    assert not [entry for entry in logs if entry["event"] == PAGE_STATE_STALL_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_page_state_stall_sees_movement_landing_between_batches() -> None:
    # A delayed render can land after one batch's after-sample and before the next batch's
    # before-sample: each batch reads internally frozen, but the page IS moving. The detector
    # compares across batches, so this healthy pattern never accumulates a stall streak.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    fp_calls = {"n": 0}

    async def between_batch_fingerprint() -> str:
        # Two samples per batch (before, after): identical within a batch, different across batches.
        fp_calls["n"] += 1
        return f"dom-{(fp_calls['n'] - 1) // 2}"

    script: list[list[tuple[str, dict[str, Any]]]] = [[("click", {"selector": f"#step-{i}"})] for i in range(10)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [click, make_finish_tool()],
            page_fingerprint=between_batch_fingerprint,
            max_action_steps=24,
            max_turns=60,
            max_tool_calls=200,
        )
    assert outcome.status == "completed"
    assert len(clicks) == 10
    nudges = [m for m in outcome.messages if m.get("role") == "user" and "unchanged" in str(m.get("content"))]
    assert nudges == []
    assert not [entry for entry in logs if entry["event"] == PAGE_STATE_STALL_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_page_state_stall_not_defeated_by_url_only_transitions() -> None:
    # history.pushState churn moves the URL without touching the document: a URL-only transition is
    # a hint, and it must not reset the stall counter while the fingerprint stays frozen.
    clicks: list[tuple[str, dict[str, Any]]] = []

    async def push_state_click(args: dict[str, Any]) -> ToolResult:
        clicks.append(("click", args))
        return ToolResult.ok("clicked", data={"page_transitioned": True})

    click = ToolSpec(
        name="click",
        description="c",
        parameters={"type": "object", "properties": {}},
        handler=push_state_click,
        billable=True,
    )

    async def frozen_fingerprint() -> str:
        return "FROZEN-DOM"

    script: list[list[tuple[str, dict[str, Any]]]] = [[("click", {"selector": f"#tab-{i}"})] for i in range(13)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [click, make_finish_tool()],
            page_fingerprint=frozen_fingerprint,
            max_action_steps=24,
            max_turns=60,
            max_tool_calls=200,
        )
    assert outcome.status == "completed"
    nudges = [m for m in outcome.messages if m.get("role") == "user" and "unchanged" in str(m.get("content"))]
    assert len(nudges) == 1
    assert [entry for entry in logs if entry["event"] == PAGE_STATE_STALL_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_page_state_stall_resets_when_new_downloads_land() -> None:
    # A download-next flow produces files without changing the DOM: a freshly detected download is
    # real progress for this detector too, so a healthy multi-download run is never nudged.
    clicks: list[tuple[str, dict[str, Any]]] = []

    async def download_click(args: dict[str, Any]) -> ToolResult:
        clicks.append(("click", args))
        return ToolResult.ok("clicked", data={"download_notice": True, "download_new": True})

    click = ToolSpec(
        name="click",
        description="c",
        parameters={"type": "object", "properties": {}},
        handler=download_click,
        billable=True,
    )

    async def frozen_fingerprint() -> str:
        return "FROZEN-DOM"

    script: list[list[tuple[str, dict[str, Any]]]] = [[("click", {"selector": f"#next-file-{i}"})] for i in range(10)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [click, make_finish_tool()],
            page_fingerprint=frozen_fingerprint,
            max_action_steps=24,
            max_turns=60,
            max_tool_calls=200,
        )
    assert outcome.status == "completed"
    nudges = [m for m in outcome.messages if m.get("role") == "user" and "unchanged" in str(m.get("content"))]
    assert nudges == []
    assert not [entry for entry in logs if entry["event"] == PAGE_STATE_STALL_SHADOW_EVENT]


def test_budget_extension_gate_vetoes_fire_independently() -> None:
    ok, reason = _budget_extension_gate(
        action_steps=10,
        last_change_evidence_step=9,
        action_warned=set(),
        progress_stalled=False,
        activity=None,
        deadline_at=None,
        extension=5,
    )
    assert ok and reason == "recent_page_change_evidence"
    assert _budget_extension_gate(10, None, set(), False, None, None, 5) == (False, "no_recent_page_change_evidence")
    assert _budget_extension_gate(10, 1, set(), False, None, None, 5) == (False, "no_recent_page_change_evidence")
    assert _budget_extension_gate(10, 9, {("click", "{}")}, False, None, None, 5) == (
        False,
        "warned_action_retry_streak",
    )
    assert _budget_extension_gate(10, 9, set(), True, None, None, 5) == (False, "no_net_progress_window")
    starving = ActivityRecency(turns_remaining=2)
    assert _budget_extension_gate(10, 9, set(), False, starving, None, 5) == (False, "insufficient_turn_headroom")
    # The turns requirement scales with the run's own observed turns-per-step burn.
    thrashy = ActivityRecency(turn=40, turns_remaining=10)
    assert _budget_extension_gate(10, 9, set(), False, thrashy, None, 5) == (False, "insufficient_turn_headroom")
    lean = ActivityRecency(turn=12, turns_remaining=10)
    assert _budget_extension_gate(10, 9, set(), False, lean, None, 5)[0]
    # Fractional burn must not be floored away: 19 turns over 10 steps is 1.9/step, so a 5-step
    # extension needs ~9.5 turns — 5 remaining cannot fund it.
    fractional = ActivityRecency(turn=19, turns_remaining=5)
    assert _budget_extension_gate(10, 9, set(), False, fractional, None, 5) == (
        False,
        "insufficient_turn_headroom",
    )
    call_starved = ActivityRecency(tool_calls_remaining=2)
    assert _budget_extension_gate(10, 9, set(), False, call_starved, None, 5) == (
        False,
        "insufficient_tool_call_headroom",
    )
    # Exactly-extension calls left funds the actions but not the terminal finish call.
    call_exact = ActivityRecency(tool_calls_remaining=5)
    assert _budget_extension_gate(10, 9, set(), False, call_exact, None, 5) == (
        False,
        "insufficient_tool_call_headroom",
    )
    token_starved = ActivityRecency(tokens_remaining=100, last_turn_tokens=50)
    assert _budget_extension_gate(10, 9, set(), False, token_starved, None, 5) == (
        False,
        "insufficient_token_headroom",
    )
    stalling = ActivityRecency(perception_stall_imminent=True)
    assert _budget_extension_gate(10, 9, set(), False, stalling, None, 5) == (False, "perception_stall_imminent")


@pytest.mark.asyncio
async def test_extension_releases_a_final_turn_granted_by_a_guard_it_just_raised() -> None:
    # The single wrap-up turn is latched by the FIRST cap trip and deliberately never re-evaluated:
    # before the budget could be extended repeatedly no cap could be relieved mid-run, so reporting
    # the remembered cap was always honest. A grant that raises that same guard past its trip breaks
    # that premise -- without the release the run ends naming a token budget it is nowhere near,
    # with the extension it just earned unspent.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", [f"page {i}" for i in range(1, 40)])
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(10):
        script.append([("observe", {})])
        script.append([("click", {"selector": f"#a{i}"})])

    with capture_logs() as logs:
        outcome, caller = await _run(
            script,
            [observe, click, make_finish_tool()],
            max_action_steps=4,
            max_turns=200,
            max_tool_calls=500,
            # Trips at the top of a turn well before the action cap, latching the wrap-up turn; the
            # grant during that turn re-derives it to 1.5M, so the latched cap stops binding.
            max_tokens=135,
            backstops_for_cap=taskv3_runaway_backstops,
        )
    released = [entry for entry in logs if entry["event"] == FINAL_TURN_RELEASED_EVENT]
    assert [entry["cap"] for entry in released] == ["max_tokens (135) reached"]
    assert outcome.cap_trip != "max_tokens (135) reached", "died on a budget the grant had relieved"
    # Releasing the counter is only half of it: the model was TOLD it was on its final turn, and that
    # message rides the transcript forever. Left standing it steers the model to wrap up early, which
    # spends the extension through the prompt instead of the counter.
    transcript = "".join(
        str(message.get("content", "")) for message in caller.message_history if message.get("role") == "user"
    )
    assert "this is the final turn before the run ends." in transcript
    assert '"budget_exhausted":false' in transcript, "the final-turn notice was never retracted"
    # The release fires MID-BATCH. A user message between an assistant turn's tool_calls and their
    # results is a transcript the provider rejects outright, which would turn every later turn into
    # a call failure and end the run as loop_error -- strictly worse than the budget_exhausted it
    # replaces. The retraction must ride out with the other end-of-batch notes.
    history = caller.message_history
    for index, message in enumerate(history):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        expected = len(message["tool_calls"])
        answers = history[index + 1 : index + 1 + expected]
        assert all(answer.get("role") == "tool" for answer in answers), (
            f"a non-tool message interrupts the tool results of turn {index}: {answers}"
        )
    # The relieved run goes on to spend the budget it earned instead of stopping one step in.
    assert len(clicks) > 4
    assert len([entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENDED_EVENT]) > 1


def test_cap_trip_relieved_only_for_the_guards_an_extension_actually_raises() -> None:
    # A release resurrects a run past a cap it was stopped at, so the set of relievable caps must be
    # exactly the three an extension re-derives. The wall clock is never re-derived, and the
    # action-step trip is not a top-of-turn cap. An integration test cannot reach the deadline case:
    # the gate's own deadline-headroom check (loop.py, "insufficient_deadline_headroom") refuses
    # every grant once the clock is blown, so a deadline latch and a grant cannot coexist in one run
    # -- that branch is defensive. This pins the predicate; the wiring above it is pinned by
    # test_extension_releases_a_final_turn_granted_by_a_guard_it_just_raised.
    kwargs = dict(
        max_tokens=1000,
        total_tokens=50,
        final_turn_token_reserve=0,
        max_turns=200,
        turns=10,
        max_tool_calls=500,
        total_tool_calls=20,
    )
    assert _cap_trip_relieved("max_tokens (100) reached", **kwargs)
    assert _cap_trip_relieved("max_turns (12) reached", **kwargs)
    assert _cap_trip_relieved("max_tool_calls (25) reached", **kwargs)
    assert not _cap_trip_relieved("deadline (1800s) reached", **kwargs)
    assert not _cap_trip_relieved("Reached the maximum steps (24)", **kwargs)
    assert not _cap_trip_relieved(None, **kwargs)
    # Still tripped is still tripped: releasing here would resurrect the run into an immediate
    # re-trip. The reserve is part of the trip, so it is part of the relief.
    assert not _cap_trip_relieved("max_tokens (100) reached", **{**kwargs, "total_tokens": 1000})
    assert not _cap_trip_relieved(
        "max_tokens (100) reached", **{**kwargs, "total_tokens": 950, "final_turn_token_reserve": 50}
    )
    assert not _cap_trip_relieved("max_turns (12) reached", **{**kwargs, "turns": 200})
    assert not _cap_trip_relieved("max_tool_calls (25) reached", **{**kwargs, "total_tool_calls": 500})


@pytest.mark.asyncio
async def test_extension_past_the_token_ceiling_reports_the_clamp() -> None:
    # The start-of-run clamp check only sees the INITIAL cap. Now that extensions can push the cap
    # past the point where the token guard stops scaling, a run can reach that ceiling mid-flight and
    # that detector would never fire -- so the grant site raises the same log_code. Driven by the
    # REAL sizing policy at a cap whose token guard is already clamped, not by a stand-in.
    base_cap = MAX_TOKENS_CEILING // MAX_TOKENS_PER_ACTION_STEP
    max_turns, max_tool_calls, max_tokens = taskv3_runaway_backstops(base_cap)
    assert max_tokens == MAX_TOKENS_CEILING, "the base cap must already sit at the token ceiling"

    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    observe = _perception_tool("observe", [f"page {i}" for i in range(1, base_cap + 40)])
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(base_cap + 4):
        script.append([("observe", {})])
        script.append([("click", {"selector": f"#a{i}"})])

    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [observe, click, make_finish_tool()],
            max_action_steps=base_cap,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            backstops_for_cap=taskv3_runaway_backstops,
        )
    assert outcome.status == "budget_exhausted", "the run must end on a budget, not an early error"
    granted = [entry for entry in logs if entry["event"] == ACTION_BUDGET_EXTENDED_EVENT]
    assert granted, "the run must actually reach a grant, or the probe cannot fire"
    assert granted[0]["max_tokens"] == MAX_TOKENS_CEILING, "tokens stop rising; turns do not"
    assert granted[0]["max_turns"] > max_turns
    clamped = [entry for entry in logs if entry.get("log_code") == "taskv3_token_backstop_clamped"]
    assert len(clamped) == 1, "reported once per run, not once per grant"
    assert clamped[0]["step_cap"] == base_cap + base_cap // 2


def test_budget_extension_gate_credits_the_headroom_the_grant_itself_creates() -> None:
    # The runaway guards are functions of the action-step budget, so granting an extension also
    # raises them. Judging the grant on PRE-grant headroom refuses extensions the grant itself pays
    # for, which is how a step-cap death gets relocated onto the token guard instead of converted.
    # The check stays a real discriminator: the gain is the guards' own per-step allowance, so a run
    # burning more than that per turn -- the re-read-the-page spiral the backstops exist for -- still
    # fails, and so does one whose gain is zero because a guard has clamped at its ceiling.
    # Scope: this pins the predicate only. Deleting the call site's `headroom_gain=` argument leaves
    # it green -- that link is covered above the seam, by tests/browser_e2e/test_taskv3_budget_extension_e2e.py.
    starved = ActivityRecency(tokens_remaining=100, last_turn_tokens=50)
    assert _budget_extension_gate(10, 9, set(), False, starved, None, 5) == (False, "insufficient_token_headroom")
    assert _budget_extension_gate(10, 9, set(), False, starved, None, 5, headroom_gain=(0, 0, 150))[0]
    assert _budget_extension_gate(10, 9, set(), False, starved, None, 5, headroom_gain=(0, 0, 149)) == (
        False,
        "insufficient_token_headroom",
    )
    turn_starved = ActivityRecency(turns_remaining=3, turn=10)
    assert _budget_extension_gate(10, 9, set(), False, turn_starved, None, 5) == (False, "insufficient_turn_headroom")
    assert _budget_extension_gate(10, 9, set(), False, turn_starved, None, 5, headroom_gain=(30, 0, 0))[0]
    call_starved = ActivityRecency(tool_calls_remaining=4)
    assert _budget_extension_gate(10, 9, set(), False, call_starved, None, 5) == (
        False,
        "insufficient_tool_call_headroom",
    )
    assert _budget_extension_gate(10, 9, set(), False, call_starved, None, 5, headroom_gain=(0, 2, 0))[0]


@pytest.mark.asyncio
async def test_on_action_round_fires_once_per_action_round() -> None:
    # The callback fires once per action ROUND (a turn with >=1 successful billable action), not per
    # tool and not on perception-only turns, and receives that round's (name, args) list plus the
    # assistant text the SAME turn produced.
    rounds: list[list[tuple[str, dict[str, Any]]]] = []
    round_texts: list[str | None] = []

    async def _on_round(actions: list[tuple[str, dict[str, Any]]], turn_text: str | None) -> None:
        rounds.append(actions)
        round_texts.append(turn_text)

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
    texts = ["looking around", "clicking the field and typing into it", "done"]
    outcome, _ = await _run(script, [observe, click, type_, make_finish_tool()], on_action_round=_on_round, texts=texts)
    assert outcome.status == "completed"
    assert len(rounds) == 1
    assert rounds[0] == [("click", {"selector": "#a"}, True), ("type", {"selector": "#b", "text": "x"}, True)]
    # The action round's text is the SECOND turn's ("clicking the field..."), not the first
    # (perception-only) or third (finish) turn's text.
    assert round_texts == [texts[1]]


@pytest.mark.asyncio
async def test_on_action_round_falls_back_to_reasoning_summary_when_text_empty() -> None:
    # Production gpt-5.6 tool calls arrive with empty message.content; the responses-bridge
    # reasoning summary (message.reasoning_content) is the only readable turn text available.
    round_texts: list[str | None] = []

    async def _on_round(_actions: list[tuple[str, dict[str, Any], bool]], turn_text: str | None) -> None:
        round_texts.append(turn_text)

    clk = []
    click = _recording_tool("click", clk)
    click.billable = True
    script = [
        [("click", {"selector": "#a"})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(
        script,
        [click, make_finish_tool()],
        on_action_round=_on_round,
        texts=[""],
        reasoning_contents=["clicked the primary submit button"],
    )
    assert outcome.status == "completed"
    assert round_texts == ["clicked the primary submit button"]


@pytest.mark.asyncio
async def test_on_action_round_prefers_content_over_reasoning_summary() -> None:
    round_texts: list[str | None] = []

    async def _on_round(_actions: list[tuple[str, dict[str, Any], bool]], turn_text: str | None) -> None:
        round_texts.append(turn_text)

    clk = []
    click = _recording_tool("click", clk)
    click.billable = True
    script = [
        [("click", {"selector": "#a"})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(
        script,
        [click, make_finish_tool()],
        on_action_round=_on_round,
        texts=["typed the field"],
        reasoning_contents=["a reasoning summary that should be ignored"],
    )
    assert outcome.status == "completed"
    assert round_texts == ["typed the field"]


@pytest.mark.asyncio
async def test_transcript_content_stays_none_when_text_empty_despite_reasoning_summary() -> None:
    # PERSISTENCE-ONLY contract: the reasoning summary reaches on_action_round (asserted above)
    # but must never enter the transcript the model re-reads next turn -- only actual message
    # content does.
    clk = []
    click = _recording_tool("click", clk)
    click.billable = True
    script = [
        [("click", {"selector": "#a"})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    outcome, _ = await _run(
        script,
        [click, make_finish_tool()],
        texts=["", "done"],
        reasoning_contents=["a readable summary that must stay out of the transcript"],
    )
    assistant_messages = [m for m in outcome.messages if m.get("role") == "assistant"]
    assert assistant_messages[0]["content"] is None


@pytest.mark.asyncio
async def test_on_action_round_fires_for_all_failed_round_with_failure_flag() -> None:
    # A dispatched billable round consumes budget even when every call errors; it must reach the
    # callback (flagged unsuccessful) so the round persists into the workflow-run step budget.
    rounds: list[list[tuple[str, dict[str, Any], bool]]] = []

    async def _on_round(actions: list[tuple[str, dict[str, Any], bool]], _turn_text: str | None) -> None:
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
    async def _boom(actions: list[tuple[str, dict[str, Any]]], _turn_text: str | None) -> None:
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
        # boom fails on "#s"; the trailing click on the SAME selector depends on it and is skipped
        # before dispatch (SKY-15143: a non-page-mutating error no longer halts the whole batch).
        [("boom", {"selector": "#s"}), ("click", {"selector": "#s"})],
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
    assert [entry["selector_present"] for entry in records] == [True, False, True, False, False]
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


def test_action_loop_terminate_threshold_is_pinned_to_its_measured_value() -> None:
    # The other action-loop tests read this constant so they track policy instead of drifting, which
    # leaves nothing asserting the VALUE — someone could set it to 50 and the suite would stay green.
    # Pinning it makes any change deliberate and visible in a diff, and sends the reader to the
    # do-not-lower note at the constant: the effective post-clearing counter was measured, showed no
    # separation between completed and stuck runs, and its highest observed value fell in a completed
    # run — so there is no positive evidence supporting a lower threshold.
    assert ACTION_LOOP_TERMINATE_AFTER == 8
    assert ACTION_LOOP_NUDGE_AFTER < ACTION_LOOP_TERMINATE_AFTER


@pytest.mark.asyncio
async def test_action_loop_survives_a_page_that_oscillates_between_two_known_states() -> None:
    # The production shape the guard was structurally blind to (SKY-14998, tsk in wr_568475173904014164):
    # one action key ran 11 times against a terminate threshold of 6, and the repeat nudge fired
    # exactly ONCE at repeat_count=3 before the run died on the token cap. A page that CYCLES rather
    # than freezes moves on every probe, so `snap.progressed` held every round and wiped the whole
    # repeat ledger — the action driving the oscillation reset its own counter forever. Returning to
    # a state this probe has already seen is not progress and must not clear the guard.
    from skyvern.forge.taskv3.loop import ACTION_LOOP_REASON_PREFIX

    panel = ["url=x text: 'filters panel open'", "url=x text: 'filters panel shut'"]
    contents = [panel[i % 2] for i in range(16)]
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for _ in range(16):
        script.append([("click", {"selector": "#apply"})])
        script.append([("observe", {})])
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), _perception_tool("observe", contents), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)

    assert outcome.status == "terminated"
    assert outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)
    assert "#apply" in outcome.reason
    # Bounded well below the 16 the script offers, and below the 11 the production run reached.
    assert len(clicks) <= 10, len(clicks)


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
    assert len(terminated) == 1 and terminated[0]["tool"] == "click"
    assert terminated[0]["repeat_count"] == ACTION_LOOP_TERMINATE_AFTER


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
        # The burst must cross the terminate threshold inside ONE turn, or the property under test
        # (no verdict before a delivered warning) is never exercised.
        [("click", {"selector": "#submit"})] * ACTION_LOOP_TERMINATE_AFTER,
        [("click", {"selector": "#submit"})],
        [("click", {"selector": "#submit"})],
    ]
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), make_finish_tool()]
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)
    # burst + 1 post-warn-queue + 1 post-warn-delivery
    assert len(clicks) == ACTION_LOOP_TERMINATE_AFTER + 2
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
    script = [[("click", {"selector": "#dead"})] for _ in range(ACTION_LOOP_TERMINATE_AFTER + 4)]
    outcome, _ = await _run(script, [click, make_finish_tool()], max_turns=200, max_tool_calls=500)
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)
    assert len(clicks) == ACTION_LOOP_TERMINATE_AFTER


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
async def test_hung_fingerprint_defers_a_completed_verdict_instead_of_stalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A renderer that never answers the settle sample must neither stall the loop past its deadline
    # nor read as settled: the sample is bounded, and a missing reading defers like a raising one.
    monkeypatch.setattr(loop_module, "_PAGE_PROBE_TIMEOUT_SECONDS", 0.01)

    async def hung_fingerprint() -> str | None:
        await asyncio.Event().wait()
        return None

    script = [
        [("finish", {"status": "completed", "reason": "done"})],
        [("finish", {"status": "completed", "reason": "done again"})],
    ]
    tools = [make_finish_tool(page_fingerprint=hung_fingerprint, settle_wait_seconds=0.0, max_settle_deferrals=1)]
    outcome, _ = await asyncio.wait_for(_run(script, tools), timeout=2)
    assert outcome.status == "completed"
    assert outcome.reason == "done again"
    assert len([m for m in outcome.messages if m.get("role") == "tool" and "still rendering" in str(m["content"])]) == 1


@pytest.mark.asyncio
async def test_hung_fingerprint_on_the_second_sample_still_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_module, "_PAGE_PROBE_TIMEOUT_SECONDS", 0.01)
    calls = {"n": 0}

    async def fingerprint() -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return "fp"
        await asyncio.Event().wait()
        return None

    script = [
        [("finish", {"status": "completed", "reason": "done"})],
        [("finish", {"status": "completed", "reason": "done again"})],
    ]
    tools = [make_finish_tool(page_fingerprint=fingerprint, settle_wait_seconds=0.0, max_settle_deferrals=1)]
    outcome, _ = await asyncio.wait_for(_run(script, tools), timeout=2)
    assert outcome.reason == "done again"
    assert len([m for m in outcome.messages if m.get("role") == "tool" and "still rendering" in str(m["content"])]) == 1


@pytest.mark.asyncio
async def test_hung_fingerprint_during_failure_evidence_still_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    # The failure-evidence quiescence wait shares the sampler: a hung sample there is unknown page
    # state, which defers (the model's re-observe is the evidence step), and must not hang the run.
    monkeypatch.setattr(loop_module, "_PAGE_PROBE_TIMEOUT_SECONDS", 0.01)
    calls = {"n": 0}

    async def fingerprint() -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return "fp"
        await asyncio.Event().wait()
        return None

    activity = ActivityRecency()
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks),
        make_finish_tool(page_fingerprint=fingerprint, activity=activity, settle_wait_seconds=0.001),
    ]
    script = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "failed", "reason": "could not submit"})],
        [("finish", {"status": "failed", "reason": "still could not submit"})],
    ]
    outcome, _ = await asyncio.wait_for(_run(script, tools, activity=activity), timeout=2)
    assert outcome.status == "failed"
    assert outcome.reason == "still could not submit"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_hung_pending_marker_probe_is_bounded_and_reads_as_nothing_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same bound on the pending-marker probe; a missing reading is not evidence of pending (the
    # gate's documented fail-open), so the verdict stands instead of the run hanging.
    monkeypatch.setattr(loop_module, "_PAGE_PROBE_TIMEOUT_SECONDS", 0.01)

    async def hung_probe(selector: str) -> str | None:
        await asyncio.Event().wait()
        return None

    watch = SubmitWatch(selector="#submit")
    script = [[("finish", {"status": "completed", "reason": "confirmation shown"})]]
    outcome, _ = await asyncio.wait_for(
        _run(script, [make_finish_tool(pending_marker=hung_probe, submit_watch=watch)]), timeout=2
    )
    assert outcome.status == "completed"
    assert outcome.reason == "confirmation shown"
    assert _held_messages(outcome) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["click_poisoning_probe", "finish_settled_probe"])
async def test_deadline_already_elapsed_bounds_every_reachable_batch_probe(scenario: str) -> None:
    # SKY-15056 exhaustive audit: two probes in the per-turn batch flow were bounded only by the flat
    # default cap (_PAGE_PROBE_TIMEOUT_SECONDS), never by what's left of the run's OWN deadline -- the
    # click-poisoning probe_before/probe_after pair (a failed batched call's page-moved check), and
    # make_finish_tool's _settled/_quiesced initial fingerprint sample. Neither took deadline_at, so a
    # hanging sampler there could run the full default timeout even with the deadline already gone.
    # No monkeypatched flat cap here -- only the deadline itself must do the bounding.
    hang_calls = {"n": 0}

    async def hanging_probe() -> str | None:
        hang_calls["n"] += 1
        await asyncio.Event().wait()  # never resolves -- proves the call site never actually awaits it
        return None

    class _SlowFirstCallCaller(_ScriptedCaller):
        async def call(self, **kwargs: Any) -> dict[str, Any]:
            if self.calls == 0:
                await asyncio.sleep(0.1)  # eats the whole deadline before this turn's batch dispatches
            return await super().call(**kwargs)

    if scenario == "click_poisoning_probe":
        clicks: list[tuple[str, dict[str, Any]]] = []
        tools: list[ToolSpec] = [_erroring_tool("click", clicks, billable=True), make_finish_tool()]
        caller: _ScriptedCaller = _SlowFirstCallCaller([[("click", {"selector": "#submit"})]])
        run_kwargs: dict[str, Any] = {"page_probe": hanging_probe, "deadline_seconds": 0.05}
    else:
        tools = [
            make_finish_tool(
                page_fingerprint=hanging_probe,
                settle_wait_seconds=0.0,
                max_settle_deferrals=1,
                deadline_at=time.monotonic() - 1.0,  # already elapsed before the run even starts
            )
        ]
        caller = _ScriptedCaller(
            [
                [("finish", {"status": "completed", "reason": "done"})],
                [("finish", {"status": "completed", "reason": "done again"})],
            ]
        )
        run_kwargs = {}

    started = time.monotonic()
    outcome = await asyncio.wait_for(
        run_agent_tool_loop(
            llm_caller=caller,
            system_prompt="sys",
            user_prompt="goal",
            tools=tools,
            max_turns=5,
            max_tool_calls=20,
            **run_kwargs,
        ),
        timeout=2,
    )
    elapsed = time.monotonic() - started

    assert elapsed <= 0.3, elapsed
    assert hang_calls["n"] == 0  # the hanging sampler was never awaited, at either call site
    assert outcome.status in ("budget_exhausted", "completed")


@pytest.mark.asyncio
async def test_pre_batch_fingerprint_sample_is_bounded_by_an_already_elapsed_deadline() -> None:
    # The stall detector's pre-batch fingerprint baseline passes deadline_at, so a slow LLM turn that
    # burns the whole deadline must skip the sample entirely -- a hanging sampler there would otherwise
    # run the full 10s probe timeout with the run already over.
    fp_calls = {"n": 0}

    async def hanging_fingerprint() -> str | None:
        fp_calls["n"] += 1
        await asyncio.sleep(5.0)
        return "fp"

    async def click_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("click done")

    tools = [
        ToolSpec(
            name="click",
            description="click",
            parameters={"type": "object", "properties": {}},
            handler=click_handler,
            billable=True,
        ),
        make_finish_tool(),
    ]

    class _SlowFirstCallCaller(_ScriptedCaller):
        async def call(self, **kwargs: Any) -> dict[str, Any]:
            if self.calls == 0:
                await asyncio.sleep(0.2)  # eats the whole deadline before this turn's batch dispatches
            return await super().call(**kwargs)

    caller = _SlowFirstCallCaller(
        [[("click", {"selector": "#next"})], [("finish", {"status": "completed", "reason": "ok"})]]
    )
    started = time.monotonic()
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=tools,
        max_turns=20,
        max_tool_calls=100,
        page_fingerprint=hanging_fingerprint,
        deadline_seconds=0.05,
    )
    elapsed = time.monotonic() - started

    # Deliberate contract change: the elapsed deadline grants one final observed turn instead of
    # ending the run, and the scripted finish on that turn wins; cap_trip carries the deadline fact.
    # The hang guard is unchanged: the sampler must stay un-awaited on the granted turn too.
    assert outcome.status == "completed"
    assert outcome.cap_trip is not None and "deadline" in outcome.cap_trip
    assert fp_calls["n"] == 0  # deadline already gone -- the hanging sampler was never awaited
    assert elapsed <= 0.3


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
async def test_payload_signed_urls_are_masked_to_their_token_across_every_tool_result_surface() -> None:
    """The single model-facing masking boundary: a resolved payload signed URL echoed by ANY tool
    result — a navigate/select/type success echo AND a handler-raised tool_error — is rewritten to
    the SAME opaque token the prompt masker minted (masking by PROVENANCE/membership, not shape),
    while a benign signing-shaped live-page URL that was never in the payload passes through
    untouched. This subsumes the per-surface masks and covers the surfaces they forgot."""
    signed = (
        "https://files.example.test/uploads/a1b2c3d4e5f6/resume.pdf"
        "?token=eyJhbGciOiJIUzI1NiJ9.c2lnbmVk.Q29ycmVjdEhvcnNlQmF0dGVyeVN0YXBsZTAxMjM0NTY3ODk"
    )
    signature_slice = "eyJhbGciOiJIUzI1NiJ9.c2lnbmVk"
    # A live-page URL that is_signed_url() flags by shape but was never in the payload.
    benign = "https://jobs.example.test/apply?token=abcdefABCDEF0123456789ghijklMNOPqrstuvwx"
    # The browser reports a payload URL back canonicalized ("/" path inserted, default port dropped),
    # which is how the real navigate tool echoes page.url — the boundary must still recognize it.
    pathless = "https://files.example.test:443?token=eyJhbGciOiJIUzI1NiJ9.cGF0aGxlc3M.Q29ycmVjdEhvcnNl"
    pathless_browser_form = "https://files.example.test/?token=eyJhbGciOiJIUzI1NiJ9.cGF0aGxlc3M.Q29ycmVjdEhvcnNl"

    refs = mask_opaque_urls({"file": signed, "link": pathless})
    token = refs.masked["file"]
    pathless_token = refs.masked["link"]

    async def navigate_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(f"navigated to {signed}, then {pathless_browser_form}.")

    async def select_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.error(f"no option matched {signed!r}")

    async def type_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(f"typed, committed {signed}")

    async def boom_handler(_args: dict[str, Any]) -> ToolResult:
        raise ValueError(f"download failed for {signed}")

    async def benign_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok(f"you are on {benign} now")

    # A redirect landing URL derived mid-task (navigate) is masked from that moment on.
    landing = "https://cdn.example.test/blob/resume.pdf?X-Amz-Signature=deadbeefdeadbeefdeadbeefdeadbeef"

    async def redirect_handler(_args: dict[str, Any]) -> ToolResult:
        refs.derive(landing)
        return ToolResult.ok(f"navigated to {landing}")

    empty = {"type": "object", "properties": {}}
    tools = [
        ToolSpec(name="redirect", description="r", parameters=empty, handler=redirect_handler),
        ToolSpec(name="navigate", description="n", parameters=empty, handler=navigate_handler),
        ToolSpec(name="select", description="s", parameters=empty, handler=select_handler),
        ToolSpec(name="type", description="t", parameters=empty, handler=type_handler),
        ToolSpec(name="boom", description="b", parameters=empty, handler=boom_handler),
        ToolSpec(name="benign", description="g", parameters=empty, handler=benign_handler),
        make_finish_tool(),
    ]
    # The loop skips the rest of a batch after a tool errors, so the error/raise surfaces each get
    # their own turn; the boundary must mask every one regardless of batching.
    script = [
        [("navigate", {}), ("type", {}), ("benign", {}), ("redirect", {})],
        [("select", {})],
        [("boom", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_opaque")
    ctx.opaque_url_refs = refs.refs
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(script, tools)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    tool_messages = {m["name"]: m["content"] for m in caller.message_history if m.get("role") == "tool"}
    # The raw signed bytes appear NOWHERE in the model-facing transcript.
    assert all(signature_slice not in m["content"] for m in caller.message_history if m.get("role") == "tool")
    assert all("cGF0aGxlc3M" not in m["content"] for m in caller.message_history if m.get("role") == "tool")
    # Every echoing surface — success and error — shows the SAME token the prompt masker minted.
    assert tool_messages["navigate"] == f"navigated to {token}, then {pathless_token}."
    assert token in tool_messages["select"]
    assert token in tool_messages["type"]
    assert token in tool_messages["boom"]
    # A benign signing-shaped live-page URL never in the payload is left intact (membership, not shape).
    assert tool_messages["benign"] == f"you are on {benign} now"
    assert "deadbeef" not in tool_messages["redirect"] and tool_messages["redirect"].startswith(
        "navigated to opaque_url_"
    )


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


def test_canonicalization_normalizes_alias_ref_values_too() -> None:
    # `data-tv3-ref="N"` is tools.py's alias handle (get_html's rewrite of a masked id), a minted
    # identity exactly like `data-tv3`; the canonicalizer only knows the `data-tv3="t..."` shape and
    # leaves `-ref` values untouched, so two get_html calls that differ only in an alias number read
    # as page churn instead of the same content.
    assert _canonical_perception_content('<input data-tv3-ref="1">') == _canonical_perception_content(
        '<input data-tv3-ref="7">'
    )
    # A cut mid-digit at the truncation boundary must canonicalize the same way as the closed form.
    assert _canonical_perception_content('<input data-tv3-ref="12') == _canonical_perception_content(
        '<input data-tv3-ref="9'
    )
    # A cut landing on the redacted "?" value must canonicalize identically to a cut on a digit.
    assert _canonical_perception_content('<input data-tv3-ref="?') == _canonical_perception_content(
        '<input data-tv3-ref="9'
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


@pytest.mark.asyncio
async def test_completion_probe_ends_loop_mid_batch_without_finish() -> None:
    # A billable action's own result can carry the download-completion signal; the probe ends the
    # run right there, no finish tool call needed, and the rest of the batch never dispatches.
    clicks: list[tuple[str, dict[str, Any]]] = []

    async def probe(_staged: frozenset[str]) -> str | None:
        return "a file finished downloading"

    tools = [_billable_tool("click", clicks), make_finish_tool()]
    script = [[("click", {"selector": "#a"}), ("click", {"selector": "#b"})]]
    recorded_rounds: list[list[tuple[str, dict[str, Any], bool]]] = []

    async def on_action_round(round_actions: list[tuple[str, dict[str, Any], bool]], _turn_text: str | None) -> None:
        recorded_rounds.append(round_actions)

    outcome, _ = await _run(script, tools, completion_probe=probe, on_action_round=on_action_round)

    assert outcome.status == "completed"
    assert outcome.reason == "a file finished downloading"
    assert len(clicks) == 1  # the second batched click never ran
    assert outcome.tool_calls == 1
    # The click that produced the download must be billed and persisted, not lost because the
    # probe fired before the recording step that appends it.
    assert outcome.billable_actions == ["click"]
    assert recorded_rounds == [[("click", {"selector": "#a"}, True)]]


@pytest.mark.asyncio
async def test_completion_probe_ignores_staged_download_unless_download_notice_too() -> None:
    # file_upload stages an http(s) source file into the same downloads dir and marks it via
    # staged_download; that must not read as the run's own landed download.
    probe_calls = 0

    async def probe(_staged: frozenset[str]) -> str | None:
        nonlocal probe_calls
        probe_calls += 1
        return "a file finished downloading"

    async def staged_only_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("uploaded", data={"staged_download": "x.pdf"})

    staged_only_tool = ToolSpec(
        name="file_upload",
        description="file_upload",
        parameters={"type": "object", "properties": {}},
        handler=staged_only_handler,
        billable=True,
    )
    outcome, _ = await _run(
        [[("file_upload", {})], [("finish", {"status": "completed", "reason": "done"})]],
        [staged_only_tool, make_finish_tool()],
        completion_probe=probe,
    )
    assert outcome.status == "completed"
    assert outcome.reason == "done"  # not the probe's reason -- it was never consulted
    assert probe_calls == 0

    async def staged_and_landed_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("uploaded", data={"staged_download": "x.pdf", "download_notice": True})

    staged_and_landed_tool = ToolSpec(
        name="file_upload",
        description="file_upload",
        parameters={"type": "object", "properties": {}},
        handler=staged_and_landed_handler,
        billable=True,
    )
    outcome2, _ = await _run(
        [[("file_upload", {})]],
        [staged_and_landed_tool, make_finish_tool()],
        completion_probe=probe,
    )
    assert outcome2.status == "completed"
    assert outcome2.reason == "a file finished downloading"
    assert probe_calls == 1


@pytest.mark.asyncio
async def test_completion_probe_gated_on_billable_or_download_notice() -> None:
    probe_calls = 0

    async def probe(_staged: frozenset[str]) -> str | None:
        nonlocal probe_calls
        probe_calls += 1
        return None

    async def observe_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("observed")

    observe_tool = ToolSpec(
        name="observe", description="observe", parameters={"type": "object", "properties": {}}, handler=observe_handler
    )

    async def check_download_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("download seen", data={"download_notice": True})

    download_tool = ToolSpec(
        name="check_download",
        description="check_download",
        parameters={"type": "object", "properties": {}},
        handler=check_download_handler,
    )

    script = [
        [("observe", {})],  # non-billable, no download_notice -> probe not consulted
        [("check_download", {})],  # non-billable but download_notice -> probe consulted
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(script, [observe_tool, download_tool, make_finish_tool()], completion_probe=probe)

    assert outcome.status == "completed"
    assert probe_calls == 1


@pytest.mark.asyncio
async def test_completion_probe_exception_is_logged_and_treated_as_none() -> None:
    async def probe(_staged: frozenset[str]) -> str | None:
        raise RuntimeError("boom")

    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", clicks), make_finish_tool()]
    script = [[("click", {"selector": "#a"})], [("finish", {"status": "completed", "reason": "done normally"})]]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, completion_probe=probe)

    assert outcome.status == "completed"
    assert outcome.reason == "done normally"
    assert any(log["log_level"] == "warning" for log in logs)


@pytest.mark.asyncio
async def test_completion_blocker_gates_completed_status_only() -> None:
    # completed: blocked once, then allowed once the blocker clears.
    responses = iter(["wait: the download has not started yet", None])

    async def blocker(_staged: frozenset[str]) -> str | None:
        return next(responses)

    tools = [make_finish_tool(completion_blocker=blocker)]
    script = [
        [("finish", {"status": "completed", "reason": "first attempt"})],
        [("finish", {"status": "completed", "reason": "second attempt"})],
    ]
    outcome, caller = await _run(script, tools)
    assert outcome.status == "completed"
    assert outcome.reason == "second attempt"
    assert caller.calls == 2  # the first finish was rejected, forcing a second turn

    # failed: the blocker is never consulted, even though it would block if asked.
    async def always_blocks(_staged: frozenset[str]) -> str | None:
        return "should never be read"

    tools2 = [make_finish_tool(completion_blocker=always_blocks)]
    outcome2, _ = await _run([[("finish", {"status": "failed", "reason": "blocked reason"})]], tools2)
    assert outcome2.status == "failed"
    assert outcome2.reason == "blocked reason"


@pytest.mark.asyncio
async def test_completion_blocker_exception_fails_closed() -> None:
    # A transient storage error checking for a landed download is evidence of nothing -- it must
    # not let a download-gated task complete with no file. finish(failed) is unaffected.
    calls = 0

    async def blocker(_staged: frozenset[str]) -> str | None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    tools = [make_finish_tool(completion_blocker=blocker)]
    script = [
        [("finish", {"status": "completed", "reason": "first attempt"})],
        [("finish", {"status": "failed", "reason": "give up"})],
    ]
    with capture_logs() as logs:
        outcome, caller = await _run(script, tools)

    assert outcome.status == "failed"
    assert outcome.reason == "give up"
    assert caller.calls == 2  # the completed attempt was rejected, forcing a second turn
    assert calls == 1  # a failed verdict never consults the blocker
    assert any(log["log_level"] == "warning" for log in logs)
    rejected_tool_messages = [
        m for m in outcome.messages if m.get("role") == "tool" and "Could not verify" in m.get("content", "")
    ]
    assert len(rejected_tool_messages) == 1


@pytest.mark.asyncio
async def test_staged_download_stays_excluded_for_the_rest_of_the_run() -> None:
    # file_upload's staged http(s) source fetch must not be treated as a landed download by any
    # LATER probe/blocker call this run -- not just skipped for the tool call that staged it.
    staged_downloads: set[str] = set()
    probe_seen: list[frozenset[str]] = []
    blocker_seen: list[frozenset[str]] = []

    async def probe(staged: frozenset[str]) -> str | None:
        probe_seen.append(staged)
        return None

    async def blocker(staged: frozenset[str]) -> str | None:
        blocker_seen.append(staged)
        return None

    async def stage_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("uploaded", data={"staged_download": "in.pdf"})

    stage_tool = ToolSpec(
        name="file_upload",
        description="file_upload",
        parameters={"type": "object", "properties": {}},
        handler=stage_handler,
        billable=True,
    )
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        stage_tool,
        _billable_tool("click", click_calls),
        make_finish_tool(completion_blocker=blocker, staged_downloads=staged_downloads),
    ]
    script = [
        [("file_upload", {})],
        [("click", {"selector": "#a"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(script, tools, completion_probe=probe, staged_downloads=staged_downloads)

    assert outcome.status == "completed"
    # The staging call itself never consults the probe (no download_notice); the next billable
    # call's probe already carries the name it staged.
    assert probe_seen == [frozenset({"in.pdf"})]
    assert blocker_seen == [frozenset({"in.pdf"})]


# --- SKY-15020 Lever C: net-progress _ProgressLedger (additive shadow) ---


def _cycling_observe(name: str, period: int) -> ToolSpec:
    """Observe fake whose content REPEATS with the given period: call i returns state (i % period).
    A period longer than PERCEPTION_RING is invisible to the ring-bounded revisit guards, which is
    the detection limit the run-scoped memory exists to lift."""
    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        state = calls["n"] % period
        calls["n"] += 1
        return ToolResult.ok(f"url=x state={state}", data={"summary": {"invalid_fields": 0}})

    return ToolSpec(
        name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler, compactable=True
    )


def _form_observe(name: str, invalid_seq: list[int]) -> ToolSpec:
    """Observe fake: call i returns UNIQUE content plus summary.invalid_fields=invalid_seq[i] (last
    value repeats). Unique content each call keeps the perception-stall / oscillation guards from
    firing, isolating the net-progress ledger as the only thing under test."""
    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        i = min(calls["n"], len(invalid_seq) - 1)
        inv = invalid_seq[i]
        calls["n"] += 1
        return ToolResult.ok(f"url=x round={calls['n']} err-{i}", data={"summary": {"invalid_fields": inv}})

    return ToolSpec(
        name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler, compactable=True
    )


@pytest.mark.asyncio
async def test_progress_ledger_shadow_fires_on_varied_action_zero_net_progress() -> None:
    # SKY-14998 shape: varied actions (a fresh selector every turn) against a form whose invalid-field
    # count never improves. Each observe differs and each click's args differ, so NONE of the three
    # repetition guards trip — yet net progress is zero, so the ledger shadow-fires (and only shadows:
    # the run is not terminated).
    rounds = 10
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", [3] * rounds), _billable_tool("click", clicks), make_finish_tool()]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    fires = [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]
    assert len(fires) == 1  # one-shot per run
    assert fires[0]["form_armed"] is True
    assert fires[0]["actions"] >= PROGRESS_LEDGER_WINDOW
    assert fires[0]["invalid_fields"] == 3
    assert outcome.status == "completed"
    assert not outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)
    assert not outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)


@pytest.mark.asyncio
async def test_progress_ledger_silent_when_invalid_fields_ratchets_down() -> None:
    # A slow-but-progressing form: the invalid-field count reaches a NEW LOW every few actions, which
    # is real net progress and resets the ledger, so it must never shadow-fire however long the run.
    invalid_seq = [6, 6, 6, 5, 5, 5, 4, 4, 4, 3, 3, 3, 2, 2, 2, 1, 1, 1, 0]
    rounds = len(invalid_seq)
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", invalid_seq), _billable_tool("click", clicks), make_finish_tool()]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_progress_ledger_never_arms_without_a_form() -> None:
    # A run with no form fields (invalid_fields always 0) has no distance-to-done metric, so the
    # ledger must never arm — the primary guard against false-fail-fast on non-form work (reading,
    # extraction) that legitimately shows no navigation for long stretches.
    rounds = 14
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", [0] * rounds), _billable_tool("click", clicks), make_finish_tool()]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_progress_ledger_silent_across_a_click_driven_multipage_form() -> None:
    # The dominant healthy shape: a multi-page application wizard. An ordinary billable "Next" click —
    # NOT the explicit navigate tool, so no page_state_changed — advances to page 2, whose fresh
    # required fields make invalid_fields RISE above page 1's floor. The run makes continuous real
    # progress (each page's count ratchets to a new low), so the ledger must stay silent; measuring
    # page 2 against page 1's minimum is the false-fire this guards. The tail stays above zero so the
    # deciding observe is form_armed — otherwise the run stays silent whether or not the rise branch
    # fires, and the test would not discriminate the branch it names (per review).
    invalid_seq = [4, 3, 2, 1, 0, 9, 9, 8, 8, 7, 7, 6, 6, 5, 5]  # page 1 ratchets to 0, page 2 rises then ratchets
    rounds = len(invalid_seq)
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", invalid_seq), _billable_tool("click", clicks), make_finish_tool()]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_progress_ledger_silent_when_downloads_keep_landing() -> None:
    # A "download next file" flow on a formful page clicks the same control many times against a page
    # whose invalid_fields never moves, but each click lands a download — hard progress that resets
    # the window, so the ledger stays silent (mirrors the action-loop guard's download exemption).
    rounds = 15
    dl: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _form_observe("observe", [3] * rounds),
        _billable_tool("download", dl, data={"download_notice": True}),
        make_finish_tool(),
    ]
    script = [[("observe", {}), ("download", {"selector": "#next-file"})] for _ in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]


def test_progress_ledger_unit_takes_the_verdict_on_a_confirming_observe() -> None:
    # No form in view → an observe never fires, however many billable actions accrue; the peak streak
    # is still tracked for the survival record.
    unarmed = _ProgressLedger()
    for _ in range(PROGRESS_LEDGER_WINDOW * 2):
        unarmed.on_billable()
    assert unarmed.observe(0) is False
    assert unarmed.peak_actions_since_progress >= PROGRESS_LEDGER_WINDOW

    # Armed and flat → the confirming observe fires once at the window, then latches.
    armed = _ProgressLedger()
    assert armed.observe(3) is False  # arms + baselines, no actions yet
    for _ in range(PROGRESS_LEDGER_WINDOW):
        armed.on_billable()
    assert armed.observe(3) is True  # a full window of actions, and this look confirms no progress
    for _ in range(PROGRESS_LEDGER_WINDOW):
        armed.on_billable()
    assert armed.observe(3) is False  # one-shot latch

    # The verdict waits for a confirming look: a window of actions batched before re-observing does
    # NOT fire, and the confirming look then shows a new low (real progress).
    deferred = _ProgressLedger()
    deferred.observe(10)
    for _ in range(PROGRESS_LEDGER_WINDOW * 3):
        deferred.on_billable()
    assert deferred.observe(2) is False

    # A rise re-baselines (a new page's fresh required fields), so the streak cannot carry across it.
    paged = _ProgressLedger()
    paged.observe(4)
    for _ in range(PROGRESS_LEDGER_WINDOW - 1):
        paged.on_billable()
    assert paged.observe(9) is False  # rose → reset + re-baseline
    for _ in range(PROGRESS_LEDGER_WINDOW - 1):
        paged.on_billable()
    assert paged.observe(9) is False  # only window-1 actions since that reset


@pytest.mark.asyncio
async def test_progress_ledger_silent_on_click_driven_equal_count_transition() -> None:
    # SKY-15020 Lever C #3 (was a false-positive): every click drives a REAL page transition
    # (page_transitioned=True) to a fresh page that happens to show the SAME invalid_fields count. The
    # real transition signal is hard progress, so the coincidentally-equal count is never read as a
    # stalled look and the ledger stays silent. Before the flag the click surfaced nothing, the equal
    # count read as flat, and the ledger false-fired on a progressing multi-page run (RED against main).
    rounds = 12
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _form_observe("observe", [3] * rounds),
        _billable_tool("click", clicks, data={"page_transitioned": True}),
        make_finish_tool(),
    ]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_progress_ledger_silent_on_url_stable_spa_advance() -> None:
    # SKY-15020 Lever C, the regression the CP ruling guards against: a URL-STABLE multi-step SPA form
    # (Workday/Greenhouse/iCIMS-style) advances page-to-page WITHOUT moving the URL, so every click
    # reports page_transitioned=False, yet each fresh step surfaces MORE required fields — a rising
    # invalid_fields count that is genuine progress. URL-unchanged does NOT prove same-page, so the
    # ledger must NEVER suppress the rise re-baseline on a False signal: the rise re-baselines exactly
    # as on main, the streak never accrues, and a healthy progressing run stays silent. RED against the
    # rejected (A) impl, which suppressed the re-baseline on False and would false-fire here.
    invalid_seq = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24]
    rounds = len(invalid_seq)
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _form_observe("observe", invalid_seq),
        _billable_tool("click", clicks, data={"page_transitioned": False}),
        make_finish_tool(),
    ]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]
    assert not outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)
    assert not outcome.reason.startswith(ACTION_LOOP_REASON_PREFIX)


@pytest.mark.asyncio
async def test_progress_ledger_documented_fn_oscillating_same_page_stays_silent() -> None:
    # SKY-15020 Lever C KNOWN LIMITATION: a genuinely-stuck form whose invalid_fields OSCILLATES on the
    # SAME page (a submit that keeps surfacing a different error set without advancing) is NOT caught.
    # page_transitioned=False cannot distinguish this oscillating-stuck run from a URL-stable SPA
    # advance (test above) — both report False with a rising count — so the ledger takes the SAFE
    # direction and re-baselines on every up-swing, exactly as on main. This documents the accepted
    # false-negative (no regression, no new FP); the same-page-oracle follow-up is what would close it.
    invalid_seq = [3, 5, 3, 5, 3, 5, 3, 5, 3, 5, 3, 5]
    rounds = len(invalid_seq)
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _form_observe("observe", invalid_seq),
        _billable_tool("click", clicks, data={"page_transitioned": False}),
        make_finish_tool(),
    ]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_ledger_fields_ride_the_terminal_telemetry_for_a_form_run() -> None:
    # Per-run terminal instrumentation: the ledger's peak no-progress streak and whether it would
    # have fired, tagged with the run's outcome — the survival-distribution data for choosing an
    # enforce threshold from data rather than gut.
    rounds = 10
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", [3] * rounds), _billable_tool("click", clicks), make_finish_tool()]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    fields = outcome.telemetry.log_fields()
    assert fields["form_ever_armed"] is True
    assert fields["peak_actions_since_progress"] >= PROGRESS_LEDGER_WINDOW
    assert fields["would_fire"] is True


@pytest.mark.asyncio
async def test_revisit_memory_sees_a_cycle_longer_than_the_perception_ring() -> None:
    # The detection limit this exists to lift: PERCEPTION_RING is 8, and its own comment says that
    # length IS the longest oscillation period that can be recognised. A run cycling with period 12
    # returns to states the ring has already evicted, so every ring-bounded guard is structurally
    # blind to it. The run-scoped memory is not.
    period = PERCEPTION_RING + 4
    rounds = period * PERCEPTION_REVISIT_LOG_AFTER + period
    tools = [_cycling_observe("observe", period), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})] for _ in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)

    assert outcome.status == "completed"
    revisits = [e for e in logs if e.get("event") == PERCEPTION_REVISIT_EVENT]
    assert revisits, "a cycle longer than the ring must still be seen"
    assert max(e["revisit_count"] for e in revisits) >= PERCEPTION_REVISIT_LOG_AFTER
    # Log-only: the run is not terminated and no verdict is taken on this signal.
    assert outcome.status == "completed"


def test_revisit_memory_refuses_new_states_at_the_cap_without_evicting_known_ones() -> None:
    # The storage bound, pinned rather than asserted in a comment. Evicting a held state to admit a
    # new one would reset the very streak the memory exists to keep, so at the cap it REFUSES new
    # states and keeps counting the ones it holds — degrading to a smaller memory, never to none.
    memory = _RevisitMemory(cap=2)
    assert memory.record("a") == (1, 0)
    assert memory.record("b") == (1, 0)
    assert memory.capped is False

    assert memory.record("c") == (0, 0), "a refused state must not report a count"
    assert memory.capped is True, "a truncated run must never read as a complete one"

    # "b" was the only state admitted after "a" was last seen, so returning to "a" has one new
    # state behind it; returning again straight away has none.
    assert memory.record("a") == (2, 1)
    assert memory.record("a") == (3, 0)
    assert memory.peak_revisits == 3
    assert memory.distinct_states == 2


@pytest.mark.asyncio
async def test_revisit_memory_stays_quiet_on_a_healthy_drill_down() -> None:
    # The false-positive case that decides whether this signal is worth collecting. A drill-down
    # returns to its list page over and over — a revisit every other touch — but opens a NEW item
    # in between, so it is progressing. Reporting it would fire identically on healthy and stuck
    # runs and leave the precision read this feeds unable to separate them.
    items = 12
    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        # list, item-0, list, item-1, list, item-2, ... the list recurs, each item is new.
        i = calls["n"]
        calls["n"] += 1
        content = "url=x list" if i % 2 == 0 else f"url=x item-{i // 2}"
        return ToolResult.ok(content, data={"summary": {"invalid_fields": 0}})

    observe = ToolSpec(
        name="observe",
        description="observe",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})] for _ in range(items * 2)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, [observe, make_finish_tool()], max_turns=200, max_tool_calls=500)

    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PERCEPTION_REVISIT_EVENT], (
        "a run covering fresh ground between returns to its list is progressing, not looping"
    )


@pytest.mark.asyncio
async def test_revisit_memory_silent_when_every_perception_state_is_new() -> None:
    # A run that never returns to a state has no revisit to report. This is the false-positive
    # direction that matters: a shadow signal headed for a future verdict must stay quiet on a run
    # that is genuinely moving through fresh pages.
    rounds = 20
    tools = [_form_observe("observe", [0] * rounds), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})] for _ in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)

    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PERCEPTION_REVISIT_EVENT]


@pytest.mark.asyncio
async def test_non_form_run_reads_false_on_the_partition_and_still_carries_canonical_touches() -> None:
    # The ledger's survival record is gated on ever_armed, so it covers only runs that saw a
    # validation error. Every search / filter / navigate / extract run — the whole non-form half of
    # the product — emitted NOTHING, which is why a fire count off that population is a floor and
    # never a prevalence. This is the complement record, keyed on the canonical tracker's counters
    # because they are the only progress signal defined without a form.
    rounds = 6
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", [0] * rounds), _billable_tool("click", clicks), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})]]
    # One batch against ONE target: touches accumulate inside the turn, so the peak does not depend
    # on whether a later observe clears the ring.
    script.append([("click", {"selector": "#stuck"}) for _ in range(4)])
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)

    assert outcome.status == "completed"
    fields = outcome.telemetry.log_fields()
    # The two branches partition the population — a non-form run must read False here, or the
    # union double-counts and the denominator is wrong in the other direction.
    assert fields["form_ever_armed"] is False
    # A ledger that exists but never armed must contribute no ledger fields either. Without this the
    # `and progress.ever_armed` conjunct can be deleted with the suite still green, and every
    # formless run would carry would_fire=False plus two zeroed counters into the denominator.
    for key in ("would_fire", "peak_actions_since_progress", "actions_since_progress", "form_armed"):
        assert key not in fields, (key, fields)
    # Load-bearing: the record has to carry the same-target churn, or it is an empty denominator.
    assert fields["peak_same_touches"] >= 4, fields


@pytest.mark.asyncio
async def test_telemetry_omits_the_ledger_fields_when_the_ledger_is_disabled() -> None:
    # The partition must be TOTAL, not conditional on an unrelated flag. The canonical tracker is
    # built and updated unconditionally, so its record does not depend on the ledger existing —
    # gating it on `progress` would drop BOTH records whenever progress_window is None and silently
    # restore the "floor, not prevalence" hole this record exists to close.
    rounds = 4
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", [3] * rounds), _billable_tool("click", clicks), make_finish_tool()]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500, progress_window=None)

    assert outcome.status == "completed"
    assert outcome.telemetry is not None
    fields = outcome.telemetry.log_fields()
    assert fields["form_ever_armed"] is False
    assert "would_fire" not in fields, fields


@pytest.mark.asyncio
async def test_telemetry_carries_the_ledger_fields_when_the_ledger_is_enabled() -> None:
    # The other half of the partition, and the compatibility guarantee: the existing record's
    # population is untouched, so run-level survival rates stay comparable across this change. Its
    # field set is not — both records gained keys.
    rounds = 10
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", [3] * rounds), _billable_tool("click", clicks), make_finish_tool()]
    script = [[("observe", {}), ("click", {"selector": f"#f{i}"})] for i in range(rounds)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)

    assert outcome.status == "completed"
    fields = outcome.telemetry.log_fields()
    assert fields["form_ever_armed"] is True
    assert "would_fire" in fields, fields


@pytest.mark.asyncio
async def test_telemetry_carries_a_count_for_every_clear_evidence_class_including_silent_ones() -> None:
    # The clear reason was LOG.debug-only, which production INFO workers never emit, so the ring's
    # clear behaviour was unmeasurable. Every class rides the terminal record with an explicit 0, so
    # "never fired" cannot be read as "field missing", and a count survives every later clear.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _form_observe("observe", [3, 3, 3]),
        _billable_tool("click", clicks, data={"page_transitioned": True}),
        make_finish_tool(),
    ]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],
        [("observe", {})],
        [("observe", {})],
        [("click", {"selector": "#next"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(
        script, tools, max_turns=200, max_tool_calls=500, semantic_commit_stats=SemanticCommitStats()
    )

    assert outcome.status == "completed"
    record = outcome.telemetry.log_fields()
    assert record["form_ever_armed"] is True
    # Two observes landed distinct content digests, then a URL-moving click cleared on the
    # transition: the earlier count must still be there after the later clear.
    assert record["clear_perception_digest"] == 2, record
    assert record["clear_page_transitioned"] == 1, record
    fired = {_ProgressEvidence.PERCEPTION_DIGEST, _ProgressEvidence.PAGE_TRANSITIONED}
    silent = {f"clear_{member.value}": 0 for member in _ProgressEvidence if member not in fired}
    assert len(silent) == 7, silent
    assert {key: record.get(key, "MISSING") for key in silent} == silent, record
    # Both groups ride the same telemetry object, so moving either off it cannot stay green.
    assert record["semantic_commit_opportunities"] == 0, record
    assert record["semantic_commit_accepts"] == 0, record


@pytest.mark.asyncio
async def test_telemetry_reports_the_peak_page_state_stall_and_not_the_streak_recovery_erased() -> None:
    # A run that stalls, recovers, then finishes leaves the live counter at 0, so a trailing read
    # reports a frozen page as never having frozen. PAGE_STATE_STALL_TERMINATE_AFTER is tuned off
    # this distribution, and a recovery-erased value biases it low, toward over-termination.
    clicks: list[tuple[str, dict[str, Any]]] = []
    click = _recording_tool("click", clicks, billable=True)
    fp_state = {"n": 0}

    async def thawing_fingerprint() -> str:
        fp_state["n"] += 1
        # Two samples per round: frozen through round 4's after-sample (8 calls), moving after.
        return "FROZEN" if fp_state["n"] <= 8 else f"dom-{fp_state['n']}"

    script: list[list[tuple[str, dict[str, Any]]]] = [[("click", {"selector": f"#try-{i}"})] for i in range(7)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(
        script,
        [click, make_finish_tool()],
        page_fingerprint=thawing_fingerprint,
        max_action_steps=24,
        max_turns=60,
        max_tool_calls=200,
    )

    assert outcome.status == "completed"
    fields = outcome.telemetry.log_fields()
    assert fields["form_ever_armed"] is False
    assert fields["peak_page_state_stall_rounds"] == 4, fields


@pytest.mark.asyncio
async def test_peak_page_state_stall_is_omitted_when_a_wired_sampler_never_reached_a_verdict() -> None:
    # A wired sampler is not a taken sample. The detector only samples behind a billable call, so a
    # run that lands none never reaches a verdict, and emitting 0 puts it in the stall-rate
    # denominator as "watched, never froze" — the same absent-vs-zero confusion one step in.
    # The sampler records instead of raising: _sample_probe swallows every exception, so a raising
    # tripwire here could never fail and the test would pass whether or not the detector sampled.
    samples: list[str] = []

    async def sampler() -> str:
        samples.append("read")
        return "dom"

    script: list[list[tuple[str, dict[str, Any]]]] = [[("finish", {"status": "completed", "reason": "done"})]]
    outcome, _ = await _run(script, [make_finish_tool()], page_fingerprint=sampler)
    assert samples == [], samples
    fields = outcome.telemetry.log_fields()
    assert "peak_page_state_stall_rounds" not in fields, fields


@pytest.mark.asyncio
async def test_peak_page_state_stall_is_omitted_with_no_sampler_and_an_explicit_zero_once_judged() -> None:
    # The two readings the field has to keep apart. Without a sampler nothing can ever tick the
    # counter, and a 0 there says "the page never froze" when it means "nothing watched it" — the
    # population an offline stall rate must drop. Once a run has actually been judged, the 0 is a
    # real measurement and must ship; that half is the only pin on the emit side of the predicate.
    script: list[list[tuple[str, dict[str, Any]]]] = [[("finish", {"status": "completed", "reason": "done"})]]
    outcome, _ = await _run(script, [make_finish_tool()])
    unwatched = outcome.telemetry.log_fields()
    assert "peak_page_state_stall_rounds" not in unwatched, unwatched

    fp_state = {"n": 0}
    judged_samples: list[str] = []

    async def moving_fingerprint() -> str:
        fp_state["n"] += 1
        judged_samples.append("read")
        return f"dom-{fp_state['n']}"

    clicks: list[tuple[str, dict[str, Any]]] = []
    judged: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#go"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    outcome, _ = await _run(
        judged,
        [_recording_tool("click", clicks, billable=True), make_finish_tool()],
        page_fingerprint=moving_fingerprint,
    )
    # Positive control for the sibling test's `samples == []`: the same recording sampler DOES get
    # read on a judged run, so an empty list there is a measured absence and not a dead probe.
    assert judged_samples, judged_samples
    watched = outcome.telemetry.log_fields()
    assert watched["peak_page_state_stall_rounds"] == 0, watched


@pytest.mark.asyncio
async def test_telemetry_carries_the_run_peak_probe_revisits_not_just_the_shadow_crossing() -> None:
    # probe_revisits is computed every snapshot but only ever logged once a probe crosses
    # stall_terminate_after, so production carries a firing indicator at that one threshold and no
    # distribution below it. Nothing can pick a different threshold from that. The run peak is the
    # instrument: it is defined for every run that took a snapshot, whatever the probe reached.
    contents = ["state-A", "state-B"] * 10
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})] for _ in range(20)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [_perception_tool("observe", contents), make_finish_tool()],
            stall_terminate_after=4,
            max_turns=200,
            max_tool_calls=500,
        )

    fields = outcome.telemetry.log_fields()
    shadow = [e for e in logs if e["event"] == PERCEPTION_STALL_SHADOW_EVENT]
    assert shadow, "the toggling probe must cross the shadow threshold, or this pins nothing"
    # The peak has to be at least what the shadow saw: the shadow reports its FIRST crossing and
    # then latches, so a peak that trailed it would be reporting less than we already log today.
    assert fields["peak_probe_revisits"] >= max(e["snapshots"] for e in shadow), (fields, shadow)


@pytest.mark.asyncio
async def test_peak_probe_revisits_is_omitted_with_no_snapshot_and_an_explicit_zero_with_one() -> None:
    # Absent and 0 are different readings, and 0 is the answer a threshold sweep would most like to
    # trust. A run that never took a perception snapshot never gave the probe a chance; a run that
    # took snapshots and never revisited a state is a real measurement of no looping.
    script: list[list[tuple[str, dict[str, Any]]]] = [[("finish", {"status": "completed", "reason": "done"})]]
    outcome, _ = await _run(script, [make_finish_tool()])
    assert "peak_probe_revisits" not in outcome.telemetry.log_fields(), outcome.telemetry.log_fields()

    # Positive control for the absence above: the same assertion on a run that DID re-read a probe
    # finds the key, so its absence is a measured absence and not a field that never ships.
    moving = [f"state-{i}" for i in range(6)]
    script = [[("observe", {})] for _ in range(6)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(
        script, [_perception_tool("observe", moving), make_finish_tool()], max_turns=200, max_tool_calls=500
    )
    fields = outcome.telemetry.log_fields()
    assert fields["peak_probe_revisits"] == 0, fields

    # A first look at a probe key has nothing to return to, so a run of one-shot probes never gave
    # the counter a chance and must read absent, not 0 — otherwise a "% of runs below any threshold"
    # statistic silently counts runs the metric was never defined on.
    script = [[("observe", {"q": i})] for i in range(5)]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(
        script,
        [_perception_tool("observe", [f"one-shot-{i}" for i in range(5)]), make_finish_tool()],
        max_turns=200,
        max_tool_calls=500,
    )
    assert "peak_probe_revisits" not in outcome.telemetry.log_fields(), outcome.telemetry.log_fields()


@pytest.mark.asyncio
async def test_peak_probe_revisits_survives_the_ledger_reset_a_navigation_performs() -> None:
    # reset() runs on every reload and revisit-navigation, and it clears the streaks by design. The
    # run-level counters must NOT go with them: adding them to reset() is a tidy-up someone will
    # reach for, and it would silently turn this into a peak-since-last-navigation.
    async def nav_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("navigated", data={"page_state_changed": True, "nav_revisit": True})

    navigate = ToolSpec(
        name="navigate", description="n", parameters={"type": "object", "properties": {}}, handler=nav_handler
    )
    # Toggle to build a peak, hop back onto known territory (which resets the ledger), then look at
    # a fresh page that never repeats. Only the pre-navigation peak can satisfy the assertion.
    contents = ["state-A", "state-B"] * 6 + [f"after-nav-{i}" for i in range(4)]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})] for _ in range(12)]
    script.append([("navigate", {"url": "https://example.test/apply"})])
    script.extend([[("observe", {})] for _ in range(4)])
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(
        script,
        [_perception_tool("observe", contents), navigate, make_finish_tool()],
        max_turns=200,
        max_tool_calls=500,
    )

    assert outcome.status == "completed"
    fields = outcome.telemetry.log_fields()
    assert fields["peak_probe_revisits"] >= 4, fields


@pytest.mark.asyncio
async def test_peak_probe_revisits_survives_a_probe_that_recovers_before_the_run_ends() -> None:
    # The load-bearing case for any peak: revisits climb while a control toggles, then reset to 0 the
    # moment the probe returns genuinely new content. A field that reported the LAST read would say
    # this run never looped, and a threshold picked off that would be tuned on recovered runs.
    contents = ["state-A", "state-B"] * 6 + [f"fresh-{i}" for i in range(8)]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})] for _ in range(len(contents))]
    script.append([("finish", {"status": "completed", "reason": "done"})])
    outcome, _ = await _run(
        script, [_perception_tool("observe", contents), make_finish_tool()], max_turns=200, max_tool_calls=500
    )

    assert outcome.status == "completed"
    fields = outcome.telemetry.log_fields()
    # The toggle phase alone reaches well past 4; the eight fresh states then drive the live counter
    # back to 0, so anything reporting the end state reads 0 here.
    assert fields["peak_probe_revisits"] >= 4, fields


@pytest.mark.asyncio
async def test_semantic_commit_counters_are_omitted_not_zeroed_when_the_run_has_no_browser_tools() -> None:
    # Absent and zero are different readings: a page-free run never builds the tools that could
    # accept, so a 0 would read as "the tier never fired" instead of "the tier did not exist".
    script: list[list[tuple[str, dict[str, Any]]]] = [[("finish", {"status": "completed", "reason": "done"})]]
    outcome, _ = await _run(script, [make_finish_tool()])
    page_free = outcome.telemetry.log_fields()
    assert "semantic_commit_opportunities" not in page_free, page_free
    assert "semantic_commit_accepts" not in page_free, page_free

    outcome, _ = await _run(script, [make_finish_tool()], semantic_commit_stats=SemanticCommitStats())
    with_tools = outcome.telemetry.log_fields()
    assert with_tools["semantic_commit_opportunities"] == 0, with_tools
    assert with_tools["semantic_commit_accepts"] == 0, with_tools
    clear_keys = {f"clear_{member.value}": 0 for member in _ProgressEvidence}
    assert {key: with_tools.get(key, "MISSING") for key in clear_keys} == clear_keys, with_tools


@pytest.mark.asyncio
async def test_progress_ledger_silent_when_actions_batch_before_a_confirming_observe() -> None:
    # Healthy batch: the model fixes several fields in one turn before re-observing (markers stay
    # valid until the page re-renders, so acting several times per observe is expected). The
    # confirming observe then reveals the invalid-field count dropped — real progress. The verdict
    # must wait for that look, never fire on the action count alone.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", [10, 2]), _billable_tool("click", clicks), make_finish_tool()]
    batch = [("click", {"selector": f"#f{i}"}) for i in range(PROGRESS_LEDGER_WINDOW)]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],
        batch,
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    assert outcome.status == "completed"
    assert not [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]


@pytest.mark.asyncio
async def test_progress_ledger_fires_at_the_confirming_observe_after_a_fruitless_batch() -> None:
    # Same batch shape, but the confirming observe shows NO improvement: the run acted a full window
    # of times and, when it finally looked, nothing advanced. The verdict lands on that observe.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [_form_observe("observe", [10, 10]), _billable_tool("click", clicks), make_finish_tool()]
    batch = [("click", {"selector": f"#f{i}"}) for i in range(PROGRESS_LEDGER_WINDOW)]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],
        batch,
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=200, max_tool_calls=500)
    fires = [e for e in logs if e.get("event") == PROGRESS_LEDGER_SHADOW_EVENT]
    assert len(fires) == 1
    assert fires[0]["actions"] >= PROGRESS_LEDGER_WINDOW
    assert outcome.status == "completed"


def _refresh_signaling_click(sink: list[tuple[str, dict[str, Any]]]) -> ToolSpec:
    """A billable click whose handler sets the same context flag a page-level handler (e.g. an
    anti-bot bypass that exhausted its retries) sets to request a reload."""

    async def handler(args: dict[str, Any]) -> ToolResult:
        sink.append(("click", args))
        ctx = skyvern_context.current()
        assert ctx is not None
        ctx.refresh_working_page = True
        return ToolResult.ok("click done")

    return ToolSpec(
        name="click",
        description="click",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        billable=True,
    )


@pytest.mark.asyncio
async def test_refresh_signal_reloads_once_clears_flag_and_skips_rest_of_batch() -> None:
    reload_calls: list[None] = []

    async def reload_page() -> None:
        reload_calls.append(None)

    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_refresh_signaling_click(click_calls), _recording_tool("type", type_calls), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"}), ("type", {"selector": "#name", "text": "x"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs:
            outcome, caller = await _run(script, tools, reload_page=reload_page)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert len(click_calls) == 1
    assert type_calls == []
    assert len(reload_calls) == 1
    assert ctx.refresh_working_page is False
    assert any(e.get("event") == "taskv3 loop honored page refresh signal" for e in logs)

    tool_messages = [m for m in caller.message_history if m.get("role") == "tool" and m.get("name") == "type"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"].startswith("skipped")
    assert "refresh" in tool_messages[0]["content"]
    user_notes = [m["content"] for m in caller.message_history if m.get("role") == "user"]
    assert any("re-observe" in note for note in user_notes)


@pytest.mark.asyncio
async def test_refresh_signal_without_reload_callback_is_consumed_without_acting() -> None:
    """With no ``reload_page`` wired the loop cannot honor the signal, so it drops it (the context
    outlives the run) and runs the batch in full."""
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    flag_when_type_ran: list[bool] = []

    async def type_handler(args: dict[str, Any]) -> ToolResult:
        type_calls.append(("type", args))
        flag_when_type_ran.append(bool(skyvern_context.current().refresh_working_page))
        return ToolResult.ok("type done")

    type_tool = ToolSpec(name="type", description="type", parameters={"type": "object"}, handler=type_handler)
    tools = [_refresh_signaling_click(click_calls), type_tool, make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"}), ("type", {"selector": "#name", "text": "x"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_no_callback")
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(script, tools)  # reload_page defaults to None
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert len(type_calls) == 1
    # Consumed at the call that raised it, not merely swept up when the run ends.
    assert flag_when_type_ran == [False]
    assert ctx.refresh_working_page is False
    contents = [str(m.get("content", "")) for m in caller.message_history]
    assert not any("refresh" in c.lower() for c in contents)


@pytest.mark.asyncio
async def test_refresh_signal_reload_failure_keeps_the_guards_and_tells_the_model() -> None:
    # A failed reload changes nothing on the page, so nothing is re-baselined; the queued calls are
    # still voided and the model hears that the reload failed rather than that the page was refreshed.
    async def reload_page() -> None:
        raise RuntimeError("reload boom")

    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_refresh_signaling_click(click_calls), _recording_tool("type", type_calls), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"}), ("type", {"selector": "#name", "text": "x"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    watch = SubmitWatch()

    ctx = SkyvernContext(task_id="tsk_refresh_reload_fails")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs:
            outcome, caller = await _run(
                script, tools, reload_page=reload_page, submit_watch=watch, max_refresh_cycles=1
            )
    finally:
        skyvern_context.reset()

    # One attempt allowed: the failed reload voids the batch and re-arms; the re-armed signal then
    # exhausts the cap on the next turn's first call and the run ends there rather than acting stale.
    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PAGE_REFRESH_EXHAUSTED_REASON_PREFIX)
    assert type_calls == []
    assert watch.selector == "#submit"
    assert ctx.refresh_working_page is False
    assert any(e.get("log_level") == "warning" for e in logs)
    contents = [str(m.get("content", "")) for m in caller.message_history]
    assert any("reload failed" in c for c in contents)
    assert not any("was refreshed" in c for c in contents)


@pytest.mark.asyncio
async def test_refresh_signal_raised_during_pre_dispatch_work_voids_the_call() -> None:
    # on_pre_action runs after the batch was chosen and before the handler; a signal raised there must
    # stop the handler from acting on the page that is gone.
    reload_calls: list[None] = []

    async def reload_page() -> None:
        reload_calls.append(None)

    async def on_pre_action(tool_name: str, args: dict[str, Any]) -> None:
        skyvern_context.current().refresh_working_page = True

    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", click_calls, billable=True), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_pre_action")
    skyvern_context.set(ctx)
    try:
        outcome, _ = await _run(script, tools, reload_page=reload_page, on_pre_action=on_pre_action)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert reload_calls == [None]
    assert click_calls == []


@pytest.mark.asyncio
async def test_no_refresh_signal_keeps_batch_intact() -> None:
    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_billable_tool("click", click_calls), _recording_tool("type", type_calls), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"}), ("type", {"selector": "#name", "text": "x"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_no_refresh")
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(script, tools, reload_page=None)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert len(type_calls) == 1
    user_notes = [m["content"] for m in caller.message_history if m.get("role") == "user"]
    assert not any("re-observe" in note for note in user_notes)


def _finish_that_also_signals_refresh() -> ToolSpec:
    """Wraps ``make_finish_tool()`` so the terminal call ITSELF is the one that leaves the refresh
    flag set -- exercising the "never voids a terminal call" branch of the honor check, which reads
    the outcome the same call just produced before deciding whether to act on the flag."""
    base = make_finish_tool()

    async def handler(args: dict[str, Any]) -> ToolResult:
        ctx = skyvern_context.current()
        assert ctx is not None
        ctx.refresh_working_page = True
        return await base.handler(args)

    return ToolSpec(
        name=base.name,
        description=base.description,
        parameters=base.parameters,
        handler=handler,
        terminal=base.terminal,
        billable=base.billable,
        recordable=base.recordable,
        compactable=base.compactable,
    )


@pytest.mark.asyncio
async def test_refresh_signal_never_voids_a_terminal_finish() -> None:
    reload_calls: list[None] = []

    async def reload_page() -> None:
        reload_calls.append(None)

    tools = [_finish_that_also_signals_refresh()]
    script: list[list[tuple[str, dict[str, Any]]]] = [[("finish", {"status": "completed", "reason": "done"})]]

    ctx = SkyvernContext(task_id="tsk_refresh_terminal")
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(script, tools, reload_page=reload_page)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert reload_calls == []
    assert ctx.refresh_working_page is False
    contents = [str(m.get("content", "")) for m in caller.message_history]
    assert not any("refresh" in c.lower() or "skipped" in c.lower() for c in contents)


@pytest.mark.asyncio
async def test_refresh_signal_left_by_one_run_does_not_leak_into_the_next() -> None:
    # The context is shared by every block of a workflow run; a signal raised at the very end of one
    # run must not reload the first page of the next.
    reload_calls: list[None] = []

    async def reload_page() -> None:
        reload_calls.append(None)

    ctx = SkyvernContext(task_id="tsk_refresh_leak")
    skyvern_context.set(ctx)
    try:
        first, _ = await _run(
            [[("finish", {"status": "completed", "reason": "done"})]],
            [_finish_that_also_signals_refresh()],
            reload_page=reload_page,
        )
        type_calls: list[tuple[str, dict[str, Any]]] = []
        second, caller = await _run(
            [
                [("click", {"selector": "#unrelated"}), ("type", {"selector": "#name", "text": "y"})],
                [("finish", {"status": "completed", "reason": "done"})],
            ],
            [_recording_tool("click", [], billable=True), _recording_tool("type", type_calls), make_finish_tool()],
            reload_page=reload_page,
        )
    finally:
        skyvern_context.reset()

    assert first.status == "completed" and second.status == "completed"
    assert reload_calls == []
    assert len(type_calls) == 1
    assert not any("refresh" in str(m.get("content", "")).lower() for m in caller.message_history)


@pytest.mark.asyncio
async def test_refresh_note_and_action_nudge_share_one_user_message() -> None:
    """A stall-nudge trigger landing in the same batch a reload FAILS must not produce two adjacent
    user-role messages -- the loop folds every note due that turn into one. (A successful reload
    discards the stall nudge instead, since it described the document that is gone.)"""

    async def reload_page() -> None:
        raise RuntimeError("reload boom")

    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _perception_tool("observe", "url=x (0 elements)"),
        _refresh_signaling_click(click_calls),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],
        [("observe", {}), ("click", {"selector": "#submit"}), ("type", {"selector": "#name", "text": "x"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_and_stall")
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(script, tools, reload_page=reload_page, stall_nudge_after=2, max_refresh_cycles=1)
    finally:
        skyvern_context.reset()

    assert outcome.status == "terminated"
    assert type_calls == []

    roles = [m.get("role") for m in caller.message_history]
    for prev_role, cur_role in zip(roles, roles[1:]):
        assert not (prev_role == "user" and cur_role == "user")

    user_notes = [m["content"] for m in caller.message_history if m.get("role") == "user"]
    failed_notes = [note for note in user_notes if "reload failed" in note]
    assert len(failed_notes) == 1
    assert "is not changing" in failed_notes[0] and "observe" in failed_notes[0]


@pytest.mark.asyncio
async def test_refresh_cycles_are_capped() -> None:
    # A handler that keeps demanding a reload is a page that cannot be stabilized: past the cap the
    # queued calls are voided instead of run on the stale page, and the run ends with that reason.
    reload_calls: list[None] = []

    async def reload_page() -> None:
        reload_calls.append(None)

    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_refresh_signaling_click(click_calls), _recording_tool("type", type_calls), make_finish_tool()]
    turn: list[tuple[str, dict[str, Any]]] = [
        ("click", {"selector": "#submit"}),
        ("type", {"selector": "#name", "text": "x"}),
    ]
    script: list[list[tuple[str, dict[str, Any]]]] = [list(turn) for _ in range(5)] + [
        [("finish", {"status": "completed", "reason": "done"})]
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_capped")
    skyvern_context.set(ctx)
    try:
        outcome, _ = await _run(script, tools, reload_page=reload_page, max_refresh_cycles=2)
    finally:
        skyvern_context.reset()

    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PAGE_REFRESH_EXHAUSTED_REASON_PREFIX)
    assert len(click_calls) == 3
    assert len(reload_calls) == 2
    assert type_calls == []
    assert ctx.refresh_working_page is False


@pytest.mark.asyncio
async def test_refresh_clears_submit_watch() -> None:
    async def reload_page() -> None:
        pass

    watch = SubmitWatch()
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_refresh_signaling_click(click_calls), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_submit_watch")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs:
            outcome, _ = await _run(script, tools, reload_page=reload_page, submit_watch=watch)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert len(click_calls) == 1
    assert any(e.get("event") == "taskv3 loop honored page refresh signal" for e in logs)
    assert watch.selector is None


@pytest.mark.asyncio
async def test_refresh_signal_raised_between_calls_is_honored_before_the_next_dispatch() -> None:
    # A route handler can raise the signal while the model's turn is in flight; the call the model
    # chose on that stale page must not run first.
    reload_calls: list[None] = []

    async def reload_page() -> None:
        reload_calls.append(None)

    click_calls: list[tuple[str, dict[str, Any]]] = []
    type_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _recording_tool("click", click_calls, billable=True),
        _recording_tool("type", type_calls),
        make_finish_tool(),
    ]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"}), ("type", {"selector": "#name", "text": "x"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_pre_dispatch")
    ctx.refresh_working_page = True
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(script, tools, reload_page=reload_page)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert reload_calls == [None]
    assert click_calls == [] and type_calls == []
    assert ctx.refresh_working_page is False
    skipped = [m for m in caller.message_history if m.get("role") == "tool" and "refreshed" in str(m.get("content"))]
    assert len(skipped) == 2
    assert any(m.get("role") == "user" and "re-observe" in str(m.get("content")) for m in caller.message_history)


@pytest.mark.asyncio
async def test_refresh_reload_is_recorded_in_the_action_round() -> None:
    # The reload is not a model tool call, but it is a page action and persists like one (a recordable,
    # non-billable round entry), succeeded or not.
    attempts: list[int] = []

    async def reload_page() -> None:
        attempts.append(len(attempts))
        if len(attempts) == 2:
            raise RuntimeError("reload boom")

    rounds: list[list[tuple[str, dict[str, Any], bool]]] = []

    async def on_round(actions: list[tuple[str, dict[str, Any], bool]], _turn_text: str | None) -> None:
        rounds.append(list(actions))

    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_refresh_signaling_click(click_calls), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#a"})],
        [("click", {"selector": "#b"})],
        [("finish", {"status": "completed", "reason": "done"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_recorded")
    skyvern_context.set(ctx)
    try:
        outcome, _ = await _run(script, tools, reload_page=reload_page, on_action_round=on_round)
    finally:
        skyvern_context.reset()

    # Turn 2's reload fails and re-arms; the retried reload on turn 3 succeeds and voids that finish.
    assert outcome.status == "completed"
    recorded = [entry for round_ in rounds for entry in round_ if entry[0] == "reload_page"]
    assert [ok for _name, _args, ok in recorded] == [True, False, True]
    assert all(args.get("reason") for _name, args, _ok in recorded)


@pytest.mark.asyncio
async def test_refresh_signal_outranks_the_action_loop_guard_on_the_same_call() -> None:
    # The repeat that would end the run is the page-level handler's cue to reload; the reload
    # re-baselines the repeat ledger, so the run continues instead of terminating.
    reload_calls: list[None] = []

    async def reload_page() -> None:
        reload_calls.append(None)

    click_calls: list[tuple[str, dict[str, Any]]] = []

    async def click_handler(args: dict[str, Any]) -> ToolResult:
        click_calls.append(("click", args))
        if len(click_calls) == 2:
            skyvern_context.current().refresh_working_page = True
        return ToolResult.ok("clicked")

    click = ToolSpec(
        name="click", description="click", parameters={"type": "object"}, handler=click_handler, billable=True
    )
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"})],
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_vs_guard")
    skyvern_context.set(ctx)
    try:
        outcome, _ = await _run(
            script,
            [click, make_finish_tool()],
            reload_page=reload_page,
            action_terminate_after=2,
            action_nudge_after=None,
        )
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert reload_calls == [None]
    assert len(click_calls) == 2


@pytest.mark.asyncio
async def test_refresh_resets_the_perception_stall_streak() -> None:
    # Two identical observes before the reload and one after: the post-reload read is a new baseline,
    # not the third of a streak that would end the run.
    async def reload_page() -> None:
        return None

    async def observe_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("<page>same</page>")

    observe = ToolSpec(
        name="observe", description="observe", parameters={"type": "object"}, handler=observe_handler, compactable=True
    )
    click_calls: list[tuple[str, dict[str, Any]]] = []
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],
        [("observe", {})],
        [("click", {"selector": "#retry"})],
        [("observe", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_perception")
    skyvern_context.set(ctx)
    try:
        outcome, _ = await _run(
            script,
            [observe, _refresh_signaling_click(click_calls), make_finish_tool()],
            reload_page=reload_page,
            stall_terminate_after=3,
        )
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_refresh_discards_a_pending_look_screenshot() -> None:
    # A look taken before the reload describes marks that no longer exist; the next call must not
    # carry its image.
    async def reload_page() -> None:
        return None

    look_calls: list[tuple[str, dict[str, Any]]] = []
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_look_tool(look_calls), _refresh_signaling_click(click_calls), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("look", {}), ("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_look")
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(script, tools, reload_page=reload_page)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert len(look_calls) == 1
    assert caller.image_blocks_per_call[1] == 0


@pytest.mark.asyncio
async def test_refresh_voided_call_is_not_charged_to_the_tool_call_budget() -> None:
    # A call voided by a refresh dispatched nothing, so the run keeps the slot for the re-observe
    # and finish the note asks for.
    async def reload_page() -> None:
        return None

    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", click_calls, billable=True), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_budget")
    ctx.refresh_working_page = True
    skyvern_context.set(ctx)
    try:
        outcome, _ = await _run(script, tools, reload_page=reload_page, max_tool_calls=1)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert click_calls == []


@pytest.mark.asyncio
async def test_refresh_re_baselines_a_dead_end_seen_earlier_in_the_batch() -> None:
    # A navigate that landed on a dead page (kept pending so a later navigate can clear it) and a
    # reload a handler requested later in the same batch: the reloaded document is the new baseline.
    async def reload_page() -> None:
        return None

    nav_calls: list[tuple[str, dict[str, Any]]] = []

    async def navigate_handler(args: dict[str, Any]) -> ToolResult:
        nav_calls.append(("navigate", args))
        return ToolResult.ok("landed", data={"navigation_dead_end": 404})

    navigate = ToolSpec(
        name="navigate", description="navigate", parameters={"type": "object"}, handler=navigate_handler, billable=True
    )
    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [navigate, _refresh_signaling_click(click_calls), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("navigate", {"url": "https://example.test/gone"}), ("click", {"selector": "#retry"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_dead_end")
    skyvern_context.set(ctx)
    try:
        outcome, _ = await _run(script, tools, reload_page=reload_page)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert len(nav_calls) == 1 and len(click_calls) == 1


@pytest.mark.asyncio
async def test_refresh_discards_a_stall_nudge_queued_before_the_reload() -> None:
    # A "the page is not changing" note about the pre-reload document must not ride along with the
    # note asking the model to re-observe the reloaded one.
    async def reload_page() -> None:
        return None

    async def observe_handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("<page>same</page>")

    observe = ToolSpec(
        name="observe", description="observe", parameters={"type": "object"}, handler=observe_handler, compactable=True
    )
    click_calls: list[tuple[str, dict[str, Any]]] = []
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("observe", {})],
        [("observe", {}), ("click", {"selector": "#retry"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_stall_nudge")
    skyvern_context.set(ctx)
    try:
        outcome, caller = await _run(
            script,
            [observe, _refresh_signaling_click(click_calls), make_finish_tool()],
            reload_page=reload_page,
            stall_nudge_after=2,
        )
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    notes = [str(m.get("content", "")) for m in caller.message_history if m.get("role") == "user"]
    assert any("re-observe" in n for n in notes)
    assert not any("is not changing" in n for n in notes)


@pytest.mark.asyncio
async def test_pending_refresh_is_consumed_before_the_pre_action_hook() -> None:
    # The pre-submit capture is a side effect; a call chosen on a page that is gone must not leave it.
    async def reload_page() -> None:
        return None

    hook_calls: list[str] = []

    async def on_pre_action(tool_name: str, args: dict[str, Any]) -> None:
        hook_calls.append(tool_name)

    click_calls: list[tuple[str, dict[str, Any]]] = []
    tools = [_recording_tool("click", click_calls, billable=True), make_finish_tool()]
    script: list[list[tuple[str, dict[str, Any]]]] = [
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]

    ctx = SkyvernContext(task_id="tsk_refresh_pre_hook")
    ctx.refresh_working_page = True
    skyvern_context.set(ctx)
    try:
        outcome, _ = await _run(script, tools, reload_page=reload_page, on_pre_action=on_pre_action)
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert hook_calls == [] and click_calls == []


@pytest.mark.asyncio
async def test_model_only_perception_stall_still_terminates_at_the_configured_threshold() -> None:
    # A run stuck re-reading an identical digest must terminate once the streak crosses the
    # configured threshold.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks, data={"page_transitioned": True}),
        _perception_tool("observe", "url=x FROZEN (1 interactive elements)"),
        make_finish_tool(),
    ]
    script = [[("click", {"selector": f"#btn{i}"}), ("observe", {})] for i in range(10)]
    outcome, _ = await _run(script, tools, stall_terminate_after=4, max_turns=30)

    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)
    assert len(clicks) < 10


@pytest.mark.asyncio
async def test_model_observe_perception_stall_still_terminates_the_same_digest_sequence() -> None:
    # Same identical-digest shape as above, over a longer run: the streak must keep terminating
    # rather than being diluted by the extra rounds.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks, data={"page_transitioned": True}),
        _perception_tool("observe", "url=x FROZEN (1 interactive elements)"),
        make_finish_tool(),
    ]
    script = [[("click", {"selector": f"#btn{i}"}), ("observe", {})] for i in range(20)]
    outcome, caller = await _run(script, tools, max_turns=30, max_tool_calls=200)

    assert outcome.status == "terminated"
    assert outcome.reason.startswith(PERCEPTION_STALL_REASON_PREFIX)
    assert len(clicks) < 20  # bounded well below the full script
    assert caller.calls <= 20


@pytest.mark.asyncio
async def test_model_observe_repeated_identical_snapshots_arms_perception_stall_imminent() -> None:
    # Approaching the stall threshold must arm perception_stall_imminent, which suppresses the
    # failure-evidence retry gate for a later submit failure.
    clicks: list[tuple[str, dict[str, Any]]] = []
    tools = [
        _billable_tool("click", clicks, data={"page_transitioned": True}),
        _perception_tool("observe", "url=x FROZEN (1 interactive elements)"),
        make_finish_tool(),
    ]
    script = [[("click", {"selector": f"#btn{i}"}), ("observe", {})] for i in range(20)]
    activity = ActivityRecency()
    outcome, _ = await _run(script, tools, activity=activity, max_turns=30, max_tool_calls=200)

    assert outcome.status == "terminated"
    assert activity.perception_stall_imminent is True


class _ReasoningDictSensitiveCaller(_ScriptedCaller):
    """Rejects any call carrying a dict reasoning_effort, as a provider without the responses
    bridge would."""

    def __init__(self, script: list[list[tuple[str, dict[str, Any]]]]) -> None:
        super().__init__(script)
        self.reasoning_per_call: list[Any] = []

    async def call(self, **kwargs: Any) -> dict[str, Any]:
        self.reasoning_per_call.append(kwargs.get("reasoning_effort"))
        if isinstance(kwargs.get("reasoning_effort"), dict):
            raise LLMProviderErrorRetryableTask("TEST_KEY")
        return await super().call(**kwargs)


@pytest.mark.asyncio
async def test_loop_drops_reasoning_dict_and_retries_the_turn_after_a_call_failure() -> None:
    # A bridge-gate false positive must degrade to the config's own reasoning_effort, not end the
    # run on turn 1.
    caller = _ReasoningDictSensitiveCaller([[("finish", {"status": "completed", "reason": "ok"})]])
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[make_finish_tool()],
        max_turns=5,
        max_tool_calls=10,
        call_kwargs={"reasoning_effort": {"effort": "high", "summary": "auto"}},
        retryable_call_exceptions=(LLMProviderErrorRetryableTask,),
        max_call_retries=2,
        call_retry_base_delay=0.0,
    )

    assert outcome.status == "completed"
    dict_calls = [r for r in caller.reasoning_per_call if isinstance(r, dict)]
    assert dict_calls and caller.reasoning_per_call[-1] is None


class _ReasoningDictOnlySensitiveCaller(_ScriptedCaller):
    """Rejects only the dict reasoning_effort; tool_choice is independently supported."""

    def __init__(self, script: list[list[tuple[str, dict[str, Any]]]]) -> None:
        super().__init__(script)
        self.kwargs_per_call: list[tuple[Any, Any]] = []

    async def call(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs_per_call.append((kwargs.get("reasoning_effort"), kwargs.get("tool_choice")))
        if isinstance(kwargs.get("reasoning_effort"), dict):
            raise LLMProviderErrorRetryableTask("TEST_KEY")
        return await super().call(**kwargs)


@pytest.mark.asyncio
async def test_degrading_the_summary_dict_keeps_tool_choice() -> None:
    caller = _ReasoningDictOnlySensitiveCaller([[("finish", {"status": "completed", "reason": "ok"})]])
    outcome = await run_agent_tool_loop(
        llm_caller=caller,
        system_prompt="sys",
        user_prompt="goal",
        tools=[make_finish_tool()],
        max_turns=5,
        max_tool_calls=10,
        call_kwargs={"reasoning_effort": {"effort": "high", "summary": "auto"}, "tool_choice": "required"},
        retryable_call_exceptions=(LLMProviderErrorRetryableTask,),
        max_call_retries=2,
        call_retry_base_delay=0.0,
    )
    assert outcome.status == "completed"
    final_reasoning, final_tool_choice = caller.kwargs_per_call[-1]
    assert final_reasoning is None
    assert final_tool_choice == "required"


def test_canonical_progress_tracker_counts_targets_and_clears_on_progress() -> None:
    from skyvern.forge.taskv3.loop import _CanonicalProgressTracker, _ProgressEvidence

    t = _CanonicalProgressTracker()
    assert t.record_touch("#code", True) == (1, 1)
    assert t.record_touch("#code", True) == (2, 2)
    assert t.record_touch("#other", False) == (1, 0)
    assert t.record_touch("#code", True) == (3, 3)
    assert t.looping_targets() == 0  # below the 4-touch rung
    assert t.record_touch("#code", False) == (4, 3)
    assert t.looping_targets() == 1
    t.progress(_ProgressEvidence.FRESH_DOWNLOAD_OR_NAVIGATION)
    assert t.record_touch("#code", True) == (1, 1)  # streak reset by progress
    assert t.looping_targets() == 0


def test_canonical_ring_state_is_touched_only_through_the_tracker() -> None:
    # The choke-point contract: a clear that bypasses progress() (poking the ring's fields
    # directly) is behavior-identical and invisible to every other test in this file, so the
    # invariant is pinned at the source level — the ring's state must have no references
    # outside _CanonicalProgressTracker's own body.
    import ast
    import inspect

    import skyvern.forge.taskv3.loop as loop_module

    source = inspect.getsource(loop_module)
    tree = ast.parse(source)
    tracker = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "_CanonicalProgressTracker"
    )
    lines = source.splitlines()
    outside = [
        (idx + 1, line)
        for idx, line in enumerate(lines)
        if ("_touches" in line or "_fired" in line) and not (tracker.lineno <= idx + 1 <= tracker.end_lineno)
    ]
    assert outside == []


def _error_billable_tool(name: str, sink: list[tuple[str, dict[str, Any]]]) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> ToolResult:
        sink.append((name, args))
        return ToolResult.error(f"{name} refused")

    return ToolSpec(
        name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler, billable=True
    )


@pytest.mark.asyncio
async def test_canonical_loop_event_fires_on_varying_args_same_target() -> None:
    # The class the incumbent action-loop key (tool+args) provably missed in prod: one selector
    # touched repeatedly with DIFFERENT args/tools, every touch refused, page unchanged. The
    # canonical tracker keys on the target and must emit its log-only event; the incumbent must NOT
    # have terminated (its exact-args streak never forms), which is the superset demonstration.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    script = [
        [("fill", {"selector": "#code", "value": "+44"})],
        [("fill", {"selector": "#code", "value": "United Kingdom"})],
        [("poke", {"selector": "#code"})],
        [("fill", {"selector": "#code", "value": "44"})],
        [("finish", {"status": "failed", "reason": "field kept refusing"})],
    ]
    tools = [_error_billable_tool("fill", touches), _error_billable_tool("poke", touches), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    assert outcome.reason == "field kept refusing"
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [e["repeat_count"] for e in fires] == [3, 4]
    assert all(e["repeat_errors"] == e["repeat_count"] for e in fires)


@pytest.mark.asyncio
async def test_canonical_loop_event_silent_when_progress_intervenes() -> None:
    # The structural safety: a confirmed progress signal (here a page transition) clears the ring,
    # so the same four touches spread across real progress never read as a loop.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    clicks: list[tuple[str, dict[str, Any]]] = []
    script = [
        [("fill", {"selector": "#code", "value": "+44"})],
        [("fill", {"selector": "#code", "value": "United Kingdom"})],
        [("advance", {"selector": "#next"})],
        [("fill", {"selector": "#code", "value": "44"})],
        [("fill", {"selector": "#code", "value": "uk"})],
        [("finish", {"status": "completed"})],
    ]
    tools = [
        _error_billable_tool("fill", touches),
        _billable_tool("advance", clicks, data={"page_transitioned": True}),
        make_finish_tool(),
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "completed"
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_canonical_loop_clears_on_invalid_fields_new_low_not_on_stall_verdict() -> None:
    # The ledger's True return is the shadow STALL verdict; the canonical clear must key on the
    # ledger re-baselining (a new low) — real form progress between refused touches stays silent.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    seq = iter([5, 4, 3, 2, 1])

    async def observe_handler(args: dict[str, Any]) -> ToolResult:
        inv = next(seq)
        # Constant observed text: the varying count must reach the ledger only via data, or the
        # perception-digest clear wipes the ring on its own and masks the clear under test.
        return ToolResult.ok("url=x form", data={"summary": {"invalid_fields": inv}})

    observe_tool = ToolSpec(
        name="observe",
        description="observe",
        parameters={"type": "object", "properties": {}},
        handler=observe_handler,
        billable=False,
        compactable=True,
    )
    script = [
        [("observe", {})],
        [("fill", {"selector": "#code", "value": "a"})],
        [("observe", {})],
        [("fill", {"selector": "#code", "value": "b"})],
        [("observe", {})],
        [("fill", {"selector": "#code", "value": "c"})],
        [("observe", {})],
        [("fill", {"selector": "#code", "value": "d"})],
        [("finish", {"status": "completed"})],
    ]
    tools = [_error_billable_tool("fill", touches), observe_tool, make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "completed"
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_canonical_loop_event_keys_marks_like_selectors() -> None:
    # look-based actions carry mark=N, not selector — the same mark re-touched must accumulate as
    # one target, not collapse into a per-tool bucket with every other mark.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    script = [
        [("poke", {"mark": 7})],
        [("poke", {"mark": 7, "value": "x"})],
        [("poke", {"mark": 7, "value": "y"})],
        [("poke", {"mark": 7, "value": "z"})],
        [("finish", {"status": "failed", "reason": "mark kept refusing"})],
    ]
    tools = [_error_billable_tool("poke", touches), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [e["repeat_count"] for e in fires] == [3, 4]


@pytest.mark.asyncio
async def test_canonical_loop_event_distinct_marks_are_distinct_targets() -> None:
    # The discriminating twin: four DIFFERENT marks are four targets — a per-tool bucket would
    # wrongly read them as one looping target.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    script = [
        [("poke", {"mark": 1})],
        [("poke", {"mark": 2})],
        [("poke", {"mark": 3})],
        [("poke", {"mark": 4})],
        [("finish", {"status": "failed", "reason": "distinct controls refused"})],
    ]
    tools = [_error_billable_tool("poke", touches), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_canonical_loop_mark_keys_die_with_the_manifest_selector_keys_survive() -> None:
    # Each look renumbers marks from 1, so a mark=1 refused after every look is a DIFFERENT control
    # each time — no streak may form across manifest generations. The same-batch selector streak is
    # the discriminating pair: its identity outlives the renumbering and must still fire.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    looks: list[tuple[str, dict[str, Any]]] = []
    turn = [("fill", {"selector": "#code", "value": "x"}), ("poke", {"mark": 1}), ("look", {})]
    script = [list(turn) for _ in range(4)] + [[("finish", {"status": "failed", "reason": "kept refusing"})]]
    tools = [
        _error_billable_tool("fill", touches),
        _error_billable_tool("poke", touches),
        _look_tool(looks),
        make_finish_tool(),
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [(e["tool"], e["repeat_count"]) for e in fires] == [("fill", 3), ("fill", 4)]


@pytest.mark.asyncio
async def test_canonical_loop_event_suppressed_when_the_completing_touch_progresses() -> None:
    # Two refusals then a third touch that lands AND changes the page: the rung-3 predicate is
    # numerically satisfied at record time (2 errors >= 3-1), but the completing touch's own
    # progress must be absorbed before the verdict — a progressing run emits nothing.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        calls["n"] += 1
        if calls["n"] < 3:
            return ToolResult.error("fill refused")
        return ToolResult.ok("fill landed", data={"page_state_changed": True})

    fill = ToolSpec(
        name="fill", description="fill", parameters={"type": "object", "properties": {}}, handler=handler, billable=True
    )
    script = [
        [("fill", {"selector": "#code", "value": "a"})],
        [("fill", {"selector": "#code", "value": "b"})],
        [("fill", {"selector": "#code", "value": "ab"})],
        [("finish", {"status": "completed", "reason": "landed"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(script, [fill, make_finish_tool()], max_turns=50, max_tool_calls=100)
    assert outcome.status == "completed"
    assert calls["n"] == 3
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_canonical_loop_cleared_by_movement_landing_between_batches() -> None:
    # A delayed render lands after one batch's after-sample and before the next batch's
    # before-sample: the cross-batch fingerprint move is confirmed progress and must clear the
    # ring BEFORE the new batch's touches are read against the old ones.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    fp_calls = {"n": 0}

    async def between_batch_fingerprint() -> str:
        fp_calls["n"] += 1
        return f"dom-{(fp_calls['n'] - 1) // 2}"

    script = [[("fill", {"selector": "#code", "value": str(i)})] for i in range(4)]
    script.append([("finish", {"status": "failed", "reason": "kept refusing"})])
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [_error_billable_tool("fill", touches), make_finish_tool()],
            page_fingerprint=between_batch_fingerprint,
            max_turns=50,
            max_tool_calls=100,
        )
    assert outcome.status == "failed"
    assert len(touches) == 4
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_canonical_loop_not_masked_by_replayed_download_notice() -> None:
    # A compactable tool replaying a retained download notice (download_notice without
    # download_new) is not fresh progress: it must not keep wiping the ring, or a post-download
    # loop could never accumulate enough touches to emit telemetry.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []

    async def replay_observe(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("page\nDownloaded: report.pdf (1.0 MB)", data={"download_notice": True})

    observe = ToolSpec(
        name="observe",
        description="observe",
        parameters={"type": "object", "properties": {}},
        handler=replay_observe,
        billable=False,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(4):
        script.append([("click", {"selector": "#dl", "note": str(i)})])
        script.append([("observe", {})])
    script.append([("finish", {"status": "failed", "reason": "kept refusing after the download"})])
    tools = [_error_billable_tool("click", touches), observe, make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    assert len(touches) == 4
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [e["repeat_count"] for e in fires] == [3, 4]


@pytest.mark.asyncio
async def test_canonical_loop_suppressed_when_completion_probe_fires_on_the_rung_touch() -> None:
    # complete_on_download: the file can land while the probe waits, AFTER the tool result returned
    # without download_new. The probe's outcome skips the end-of-batch detector, but the pending
    # rung minted by that same touch must not be emitted — the run completed on real progress.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        calls["n"] += 1
        return ToolResult.error("click refused") if calls["n"] < 3 else ToolResult.ok("clicked")

    click = ToolSpec(
        name="click",
        description="click",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        billable=True,
    )

    async def probe(_staged: frozenset[str]) -> str | None:
        return "a file finished downloading" if calls["n"] >= 3 else None

    script = [
        [("click", {"selector": "#dl", "note": "a"})],
        [("click", {"selector": "#dl", "note": "b"})],
        [("click", {"selector": "#dl", "note": "c"})],
        [("finish", {"status": "failed", "reason": "unreached"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script, [click, make_finish_tool()], completion_probe=probe, max_turns=50, max_tool_calls=100
        )
    assert outcome.status == "completed"
    assert outcome.reason == "a file finished downloading"
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_canonical_loop_not_masked_by_missing_probe_samples() -> None:
    # A missing probe sample around a failed dispatch is not evidence of a page change: intermittent
    # probe timeouts must not keep clearing the ring and permanently mask a genuine loop.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []

    async def dead_probe() -> str | None:
        return None

    async def observe_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("url=x page digest (1 interactive elements)")

    observe = ToolSpec(
        name="observe",
        description="observe",
        parameters={"type": "object", "properties": {}},
        handler=observe_handler,
        compactable=True,
    )
    script = [[("click", {"selector": "#dl", "note": str(i)})] for i in range(4)]
    script.append([("finish", {"status": "failed", "reason": "kept refusing"})])
    tools = [_error_billable_tool("click", touches), observe, make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, page_probe=dead_probe, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    assert len(touches) == 4
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [e["repeat_count"] for e in fires] == [3, 4]


@pytest.mark.asyncio
async def test_canonical_loop_absorbs_movement_in_a_terminal_batch_before_emitting() -> None:
    # Two refusals, then a batch whose successful third touch changes the DOM (fingerprint-only)
    # and whose finish completes the run: the terminal outcome skips the detector, but the batch's
    # own movement must still be absorbed before the pending rung is decided.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    calls = {"n": 0}

    async def handler(args: dict[str, Any]) -> ToolResult:
        calls["n"] += 1
        return ToolResult.error("click refused") if calls["n"] < 3 else ToolResult.ok("clicked")

    click = ToolSpec(
        name="click",
        description="click",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        billable=True,
    )
    fp_calls = {"n": 0}

    async def fingerprint() -> str:
        # Moves only at the terminal batch's after-sample (calls 1-5 are the three before-samples
        # and the first two batches' after-samples).
        fp_calls["n"] += 1
        return "dom-0" if fp_calls["n"] <= 5 else "dom-1"

    script = [
        [("click", {"selector": "#dl", "note": "a"})],
        [("click", {"selector": "#dl", "note": "b"})],
        [("click", {"selector": "#dl", "note": "c"}), ("finish", {"status": "completed", "reason": "done"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script, [click, make_finish_tool()], page_fingerprint=fingerprint, max_turns=50, max_tool_calls=100
        )
    assert outcome.status == "completed"
    assert calls["n"] == 3
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_canceled_run_emits_pending_fire_without_a_terminal_probe() -> None:
    # An acknowledged cancellation must not wait on the terminal-batch reconciliation sample (a
    # hung renderer can hold that probe for its full timeout); the pending rung is emitted as
    # minted, since no progress evidence contradicts it.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    fp_calls = {"n": 0}

    async def fingerprint() -> str:
        fp_calls["n"] += 1
        return "dom-0"

    async def should_cancel() -> bool:
        return len(touches) >= 3

    script = [
        [("click", {"selector": "#dl", "note": "a"})],
        [("click", {"selector": "#dl", "note": "b"})],
        [("click", {"selector": "#dl", "note": "c"}), ("click", {"selector": "#dl", "note": "d"})],
    ]
    with capture_logs() as logs:
        outcome, _ = await _run(
            script,
            [_error_billable_tool("click", touches), make_finish_tool()],
            page_fingerprint=fingerprint,
            should_cancel=should_cancel,
            max_turns=50,
            max_tool_calls=100,
        )
    assert outcome.status == "canceled"
    assert len(touches) == 3  # the fourth call was refused by the cancellation check
    # 2 samples per completed batch plus the final batch's before-sample; NO terminal probe.
    assert fp_calls["n"] == 5
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [e["repeat_count"] for e in fires] == [3]


@pytest.mark.asyncio
async def test_canonical_loop_cleared_by_changed_perception_digest_without_fingerprint() -> None:
    # With no page_fingerprint, a repeated observe whose digest changes is the only movement
    # evidence there is; two landed digests that differ must clear the ring like a fingerprint
    # mismatch would, so touches against superseded pages never alias into a rung.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    pages = {"n": 0}

    async def observe_handler(_args: dict[str, Any]) -> ToolResult:
        pages["n"] += 1
        return ToolResult.ok(f"url=x page {pages['n']} content (1 interactive elements)")

    observe = ToolSpec(
        name="observe",
        description="observe",
        parameters={"type": "object", "properties": {}},
        handler=observe_handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = [[("observe", {})]]
    for i in range(4):
        script.append([("click", {"selector": "#dl", "note": str(i)})])
        script.append([("observe", {})])
    script.append([("finish", {"status": "failed", "reason": "kept refusing"})])
    tools = [_error_billable_tool("click", touches), observe, make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    assert len(touches) == 4
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_canonical_rung_fires_once_even_when_the_window_parks_on_it() -> None:
    # Eight failed touches on A, two on B, then more on A: eviction keeps A's in-window count
    # parked at 8, which must not re-emit rung 8 on every subsequent touch — a rung is one
    # threshold crossing per generation.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    script = [[("click", {"selector": "#a", "note": str(i)})] for i in range(8)]
    script += [[("click", {"selector": "#b", "note": str(i)})] for i in range(2)]
    script += [[("click", {"selector": "#a", "note": f"again-{i}"})] for i in range(3)]
    script.append([("finish", {"status": "failed", "reason": "kept refusing"})])
    tools = [_error_billable_tool("click", touches), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=60, max_tool_calls=200)
    assert outcome.status == "failed"
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [e["repeat_count"] for e in fires] == [3, 4, 6, 8]


@pytest.mark.asyncio
async def test_canonical_loop_clears_on_new_low_even_under_a_replayed_notice() -> None:
    # Blocker 1 (AronPerez round 6/10): a replayed download notice hard-progresses the shadow
    # ledger, nulling invalid_baseline BEFORE the shadow read on the same result — which left the
    # new-low clear dead for the rest of the page. Constant observe content keeps the perception
    # digest from masking the probe. A form ratcheting to a new low on every look is progressing;
    # the refusing selector must emit nothing.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    seq = iter([6, 5, 4, 3, 2])

    async def observe_handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("url=x form", data={"download_notice": True, "summary": {"invalid_fields": next(seq)}})

    observe = ToolSpec(
        name="observe",
        description="observe",
        parameters={"type": "object", "properties": {}},
        handler=observe_handler,
        compactable=True,
    )
    script: list[list[tuple[str, dict[str, Any]]]] = []
    for i in range(4):
        script.append([("observe", {})])
        script.append([("click", {"selector": "#stuck", "note": str(i)})])
    script.append([("observe", {})])
    script.append([("finish", {"status": "failed", "reason": "one field kept refusing"})])
    tools = [_error_billable_tool("click", touches), observe, make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    assert len(touches) == 4
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


@pytest.mark.asyncio
async def test_completion_probe_spares_a_sibling_targets_rung() -> None:
    # Blocker 2 (AronPerez round 10): the probe firing is progress for the COMPLETING touch only.
    # A five-error streak on one selector minted a genuine rung 6 the same batch a download on a
    # DIFFERENT selector completed the run — that sibling rung is real data and must survive.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    fetched = {"done": False}

    async def fetch_handler(_args: dict[str, Any]) -> ToolResult:
        fetched["done"] = True
        return ToolResult.ok("fetch dispatched")

    fetch = ToolSpec(
        name="fetch",
        description="fetch",
        parameters={"type": "object", "properties": {}},
        handler=fetch_handler,
        billable=True,
    )

    async def probe(_staged: frozenset[str]) -> str | None:
        return "a file finished downloading" if fetched["done"] else None

    script = [[("click", {"selector": "#stuck", "note": str(i)})] for i in range(5)]
    script.append([("click", {"selector": "#stuck", "note": "sixth"}), ("fetch", {"selector": "#other"})])
    tools = [_error_billable_tool("click", touches), fetch, make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, completion_probe=probe, max_turns=50, max_tool_calls=100)
    assert outcome.status == "completed"
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [e["repeat_count"] for e in fires] == [3, 4, 6]


@pytest.mark.asyncio
async def test_canonical_rung_rearms_after_full_eviction() -> None:
    # A rung re-arms once the target's in-window count dips below it: a fresh streak after full
    # eviction is a genuine re-crossing, unlike the parked-window case the once-per-generation
    # guard exists for.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    script = [[("click", {"selector": "#a", "note": str(i)})] for i in range(3)]
    script += [[("click", {"selector": "#b", "note": str(i)})] for i in range(10)]
    script += [[("click", {"selector": "#a", "note": f"again-{i}"})] for i in range(3)]
    script.append([("finish", {"status": "failed", "reason": "kept refusing"})])
    tools = [_error_billable_tool("click", touches), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, max_turns=60, max_tool_calls=200)
    assert outcome.status == "failed"
    fires = [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT]
    assert [e["repeat_count"] for e in fires] == [3, 3, 4, 6, 8, 3]


@pytest.mark.asyncio
async def test_canonical_loop_cleared_by_positive_probe_mismatch_on_a_failed_call() -> None:
    # A failed call that moved the document (two landed identity samples that differ) is progress
    # the fingerprint can miss entirely (a same-template step renders identically): the poisoned
    # batch stop already knows the page moved, and the ring must learn it too.
    from skyvern.forge.taskv3.loop import CANONICAL_LOOP_EVENT

    touches: list[tuple[str, dict[str, Any]]] = []
    probe_calls = {"n": 0}

    async def moving_probe() -> str:
        probe_calls["n"] += 1
        return f"doc-{probe_calls['n']}"

    script = [[("click", {"selector": "#stuck", "note": str(i)})] for i in range(4)]
    script.append([("finish", {"status": "failed", "reason": "kept refusing"})])
    tools = [_error_billable_tool("click", touches), make_finish_tool()]
    with capture_logs() as logs:
        outcome, _ = await _run(script, tools, page_probe=moving_probe, max_turns=50, max_tool_calls=100)
    assert outcome.status == "failed"
    assert len(touches) == 4
    assert [e for e in logs if e.get("event") == CANONICAL_LOOP_EVENT] == []


_COVERAGE_CODES = {
    "COVERAGE_NOT_ACTIVE": "The coverage tab loaded but the member has no active plan today.",
    "COVERAGE_TAB_UNAVAILABLE": "The coverage tab could not be rendered because of a portal-side failure.",
}


@pytest.mark.asyncio
async def test_finish_offers_configured_codes_and_carries_a_deliberate_choice() -> None:
    # v1 shows the model the user's codes in-loop, so a v1 terminal verdict names its own code. v3
    # did not, and codes were matched on afterwards instead -- so a block that never reasoned about
    # coverage could still be handed a coverage outcome (SKY-15586). The model must be able to say it.
    finish = make_finish_tool(error_code_mapping=_COVERAGE_CODES)
    assert finish.parameters["properties"]["error_code"]["enum"] == sorted(_COVERAGE_CODES)

    script = [[("finish", {"status": "terminated", "reason": "no active plan", "error_code": "COVERAGE_NOT_ACTIVE"})]]
    outcome, _ = await _run(script, [finish])
    assert outcome.status == "terminated"
    assert outcome.error_code == "COVERAGE_NOT_ACTIVE"
    assert outcome.error_codes_offered is True


@pytest.mark.asyncio
async def test_a_block_with_no_business_outcome_acquires_no_code() -> None:
    # The load-bearing case: a block that reaches a terminal state for a reason none of the codes
    # describes must end with NO code. Today nothing asks the model, so a detector matches one on
    # from the page afterwards; once asked, a verdict that names none must REPORT none -- and stay
    # distinguishable from a task that was never offered any.
    finish = make_finish_tool(error_code_mapping=_COVERAGE_CODES)
    script = [[("finish", {"status": "terminated", "reason": "could not reach the logout control"})]]
    outcome, _ = await _run(script, [finish])
    assert outcome.error_code is None
    assert outcome.error_codes_offered is True  # asked and declined -- not merely unasked


@pytest.mark.asyncio
async def test_a_code_the_user_never_defined_is_refused() -> None:
    # Same guarantee filter_to_user_defined_codes gives the step engine: only configured codes may
    # reach the customer's webhooks, whatever the model returns.
    finish = make_finish_tool(error_code_mapping=_COVERAGE_CODES)
    script = [[("finish", {"status": "terminated", "reason": "made one up", "error_code": "INVENTED_CODE"})]]
    outcome, _ = await _run(script, [finish])
    assert outcome.error_code is None


@pytest.mark.asyncio
async def test_a_task_without_configured_codes_keeps_todays_finish_schema() -> None:
    # Out-of-scope guard: tasks with no mapping must see byte-identical finish parameters, so this
    # change cannot alter behavior for the overwhelming majority of runs.
    assert "error_code" not in make_finish_tool().parameters["properties"]
    assert make_finish_tool().parameters == make_finish_tool(error_code_mapping=None).parameters

    script = [[("finish", {"status": "terminated", "reason": "done"})]]
    outcome, _ = await _run(script, [make_finish_tool()])
    assert outcome.error_code is None
    assert outcome.error_codes_offered is False  # never asked -- the detector must stay in charge


@pytest.mark.asyncio
async def test_a_code_is_accepted_on_a_failed_finish_too() -> None:
    # The schema and the goal must agree that a code is about WHAT HAPPENED, not about which status
    # was reached. A schema saying "terminated only" would both re-create the pressure to terminate
    # in order to fit a code, and -- since the detector is now skipped whenever codes were offered --
    # leave a model-declared failure with no user-defined code at all.
    finish = make_finish_tool(error_code_mapping=_COVERAGE_CODES)
    # Assert the absence of ONLY-ness, not of the word: accurate future wording may well mention a
    # status, and pinning the word would block it.
    assert "ONLY with status" not in finish.parameters["properties"]["error_code"]["description"]
    # The twin of the goal text, and it drifts the same way: an example of OUR failure that reads
    # as a broken page would tell the model to withhold the very code the site problem earns.
    assert "losing the page" not in finish.parameters["properties"]["error_code"]["description"]

    script = [[("finish", {"status": "failed", "reason": "no active plan", "error_code": "COVERAGE_NOT_ACTIVE"})]]
    outcome, _ = await _run(script, [finish])
    assert outcome.status == "failed"
    assert outcome.error_code == "COVERAGE_NOT_ACTIVE"
