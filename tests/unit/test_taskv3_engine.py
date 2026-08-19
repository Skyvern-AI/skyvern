"""Unit tests for the native Task V3 engine (prompt + tools + loop assembly).

Reuses the scripted fake LLMCaller from the loop test and the fake Playwright page
from the tools test, so the engine's wiring is exercised without a real LLM or browser.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from skyvern.forge.taskv3.engine import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    MAX_TOOL_CALLS_PER_ACTION_STEP,
    MAX_TURNS_PER_ACTION_STEP,
    coerce_v3_parameters,
    run_task_v3_agent_loop,
    taskv3_runaway_backstops,
)
from skyvern.forge.taskv3.tools import PAGE_UNAVAILABLE_ERROR
from tests.unit.test_taskv3_loop import _ScriptedCaller
from tests.unit.test_taskv3_tools import _FakePage, _fixed_page_provider


@pytest.mark.asyncio
async def test_engine_completes_after_acting() -> None:
    # observe -> type -> finish(completed): the first finish is accepted (no forced extra turn).
    script = [
        [("observe", {})],
        [("type", {"selector": "#first", "text": "John"})],
        [("finish", {"status": "completed", "reason": "filled, ready to submit"})],
    ]
    caller = _ScriptedCaller(script)
    page = _FakePage()
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(page),
        llm_caller=caller,
        goal="Fill the application form and stop before submitting.",
        parameters={"first_name": "John"},
        starting_url="https://example.test/apply",
    )
    assert outcome.status == "completed"
    assert outcome.reason == "filled, ready to submit"
    assert outcome.turns == 3
    # The fill actually dispatched to the page.
    assert any(c[0] == "fill" and c[1]["selector"] == "#first" for c in page.calls)


@pytest.mark.asyncio
async def test_engine_accepts_first_finish() -> None:
    script = [[("finish", {"status": "completed", "reason": "done"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="noop"
    )
    assert outcome.status == "completed" and outcome.turns == 1


@pytest.mark.asyncio
async def test_engine_terminate_accepted_immediately() -> None:
    script = [[("finish", {"status": "terminated", "reason": "CAPTCHA blocks the form"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="apply"
    )
    assert outcome.status == "terminated" and outcome.turns == 1


@pytest.mark.asyncio
async def test_engine_exposes_browser_and_finish_tools_no_task_ecosystem() -> None:
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "x"})]])
    await run_task_v3_agent_loop(page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="x")
    sent = {t["function"]["name"] for t in (caller.sent_tools or [])}
    assert {"observe", "type", "click", "file_upload", "finish"} <= sent
    assert not ({"act", "extract", "validate", "run_task", "login"} & sent)


@pytest.mark.asyncio
async def test_engine_records_billable_actions() -> None:
    # observe/finish are not billable; type + click are — so per-action billing counts 2.
    script = [
        [("observe", {})],
        [("type", {"selector": "#first", "text": "John"})],
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="apply"
    )
    assert outcome.status == "completed"
    assert outcome.billable_actions == ["type", "click"]


@pytest.mark.asyncio
async def test_engine_wires_budget_and_retry_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The engine must pass real cost ceilings + transient-retry policy to the loop by default,
    # so the wired path (which passes neither) inherits them. Pins the defaults against regression.
    from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x"
    )

    assert captured["max_tokens"] == engine_mod.DEFAULT_MAX_TOKENS
    assert captured["deadline_seconds"] == engine_mod.DEFAULT_DEADLINE_SECONDS
    assert captured["max_call_retries"] == engine_mod.DEFAULT_MAX_CALL_RETRIES
    assert captured["retryable_call_exceptions"] == (LLMProviderErrorRetryableTask,)


def test_runaway_backstops_scale_with_action_step_budget() -> None:
    # No action-step budget -> the guards are the engine's fixed defaults.
    assert taskv3_runaway_backstops(None) == (DEFAULT_MAX_TURNS, DEFAULT_MAX_TOOL_CALLS)
    assert taskv3_runaway_backstops(0) == (DEFAULT_MAX_TURNS, DEFAULT_MAX_TOOL_CALLS)
    # Small cap: the fixed floors dominate, so a productive run keeps its historical headroom.
    assert taskv3_runaway_backstops(10) == (DEFAULT_MAX_TURNS, DEFAULT_MAX_TOOL_CALLS)
    # Large cap: both guards scale up so the action-step budget -- not the guards -- bounds the run.
    big = 100
    assert taskv3_runaway_backstops(big) == (
        big * MAX_TURNS_PER_ACTION_STEP,
        big * MAX_TOOL_CALLS_PER_ACTION_STEP,
    )
    # Monotonic: a larger cap never yields smaller guards.
    t_small, c_small = taskv3_runaway_backstops(20)
    t_big, c_big = taskv3_runaway_backstops(80)
    assert t_big >= t_small and c_big >= c_small


@pytest.mark.asyncio
async def test_engine_page_lost_fails_cleanly_not_hang() -> None:
    # A provider that never resolves a page (browser truly gone): every browser tool call errors
    # with a browser-lost reason, and the loop's existing action-step/turn backstops guarantee a
    # bounded, clean failure -- no hang, and no new termination mechanism was needed for it.
    async def gone_provider() -> Any:
        return None

    script = [[("click", {"selector": "#x"})]] * 10  # keeps retrying; would never finish on its own
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=gone_provider,
        llm_caller=caller,
        goal="apply",
        max_action_steps=2,
        max_turns=20,
    )
    assert outcome.status == "budget_exhausted"
    tool_messages = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m["content"] == PAGE_UNAVAILABLE_ERROR for m in tool_messages)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"full_name": "Ada", "email": "a@x.test"}, {"full_name": "Ada", "email": "a@x.test"}),
        # JSON object stored as a string (single-encoded): parsed so the profile reaches the model
        # instead of being dropped to None by an isinstance(dict) check (the org-at-0% regression).
        ('{"full_name": "Ada", "email": "a@x.test"}', {"full_name": "Ada", "email": "a@x.test"}),
        # Double-encoded (json.dumps of the single-encoded string): both layers unwrapped.
        (json.dumps('{"full_name": "Ada", "email": "a@x.test"}'), {"full_name": "Ada", "email": "a@x.test"}),
        (None, None),
        ("", None),
        ("   ", None),
        ("null", None),  # JSON null is genuinely no payload, not {"task_data": None}
        ("just a plain string", {"task_data": "just a plain string"}),
        (["a", "b"], {"task_data": ["a", "b"]}),
    ],
)
def test_coerce_v3_parameters_surfaces_payload_regardless_of_type(payload: object, expected: object) -> None:
    assert coerce_v3_parameters(payload) == expected


@pytest.mark.asyncio
async def test_page_free_mode_has_no_browser_tools_and_page_free_prompt() -> None:
    # Structural, not advisory: a page-free run exposes no perception/action tools, its system
    # prompt never instructs observing, and an attempted observe is an unknown tool.
    script = [[("observe", {})], [("finish", {"status": "completed", "reason": "criteria hold"})]]
    caller = _ScriptedCaller(script)

    async def no_page() -> Any:
        raise AssertionError("page provider must never be consulted in page-free mode")

    outcome = await run_task_v3_agent_loop(
        page_provider=no_page,
        llm_caller=caller,
        goal="assess",
        page_free=True,
        max_action_steps=2,
        max_turns=6,
    )
    assert outcome.status == "completed"
    tool_messages = [m["content"] for m in outcome.messages if m.get("role") == "tool"]
    assert any("unknown_tool: observe" in c for c in tool_messages)
    system_message = next(m for m in outcome.messages if m.get("role") == "system")
    assert "NO browser tools" in system_message["content"]
    assert "Perceive with" not in system_message["content"]


@pytest.mark.asyncio
async def test_engine_defers_completion_while_delayed_render_settles() -> None:
    # Regression for the fixture's delayed-states pattern: a panel's data loads AFTER a delay, and
    # a completion verdict issued mid-render must be deferred until two DOM samples match. The fake
    # page mutates its DOM signature once (the delayed load landing), like the fixture's
    # loading -> loaded transition.
    settle_results = iter([False, True])

    async def settle_probe() -> bool:
        # Models the fixture's delayed load: the first sample pair differs (mid-render), the
        # second matches (loaded panel).
        return next(settle_results, True)

    async def provider() -> Any:
        return object()

    script = [
        [("finish", {"status": "completed", "reason": "panel visible"})],
        [("finish", {"status": "completed", "reason": "panel content confirmed"})],
    ]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=provider,
        llm_caller=caller,
        goal="open the panel",
        settle_probe=settle_probe,
        max_action_steps=2,
        max_turns=8,
    )
    assert outcome.status == "completed"
    assert outcome.reason == "panel content confirmed"


@pytest.mark.asyncio
async def test_page_free_mode_finishes_without_settle_probe() -> None:
    # Page-free runs have no page to settle: finish(completed) is immediate and the provider is
    # never consulted.
    async def no_page() -> Any:
        raise AssertionError("provider must not be consulted in page-free mode")

    script = [[("finish", {"status": "completed", "reason": "criteria hold"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=no_page,
        llm_caller=caller,
        goal="assess",
        page_free=True,
        max_action_steps=2,
        max_turns=4,
    )
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_bare_run_finishes_without_settle_probe() -> None:
    # Fenced: without a settle_probe (the bare-task default) finish(completed) never consults
    # the page, preserving the live bare-task arm's finish path.
    probe_calls = 0

    async def counting_probe() -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return False

    async def provider() -> Any:
        return object()

    script = [[("finish", {"status": "completed", "reason": "done"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=provider, llm_caller=caller, goal="g", max_action_steps=2, max_turns=4
    )
    assert outcome.status == "completed"
    assert probe_calls == 0


@pytest.mark.asyncio
async def test_engine_omits_tool_choice_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)
    monkeypatch.setattr(engine_mod.settings, "TASK_V3_TOOL_CHOICE_REQUIRED", False)

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x"
    )
    # None, not {} -- the loop splats **(call_kwargs or {}), so preserving None keeps the
    # default (lever-off) path byte-identical to before this lever existed.
    assert captured["call_kwargs"] is None

    step = object()
    captured.clear()
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x", step=step
    )
    assert captured["call_kwargs"] == {"step": step}


@pytest.mark.asyncio
async def test_engine_requests_tool_choice_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)
    monkeypatch.setattr(engine_mod.settings, "TASK_V3_TOOL_CHOICE_REQUIRED", True)

    step = object()
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x", step=step
    )

    assert captured["call_kwargs"] == {"step": step, "tool_choice": "required"}

    # The engine asking the caller is what keeps tool_choice_in_effect honest rather than
    # aspirational: a model that cannot take the parameter must not have it added at all.
    class _UnsupportedCaller(_ScriptedCaller):
        def supports_tool_choice(self) -> bool:
            return False

    captured.clear()
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_UnsupportedCaller([]), goal="x", step=step
    )

    assert captured["call_kwargs"] == {"step": step}
