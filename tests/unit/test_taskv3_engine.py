"""Unit tests for the native Task V3 engine (prompt + tools + loop assembly).

Reuses the scripted fake LLMCaller from the loop test and the fake Playwright page
from the tools test, so the engine's wiring is exercised without a real LLM or browser.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
import yarl
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.taskv3 import engine as engine_mod
from skyvern.forge.taskv3.engine import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    MAX_TOOL_CALLS_PER_ACTION_STEP,
    MAX_TURNS_PER_ACTION_STEP,
    coerce_v3_parameters,
    run_task_v3_agent_loop,
    taskv3_runaway_backstops,
)
from skyvern.forge.taskv3.loop import LoopOutcome, SemanticCommitStats, ToolResult, ToolSpec, _ProgressEvidence
from skyvern.forge.taskv3.opaque_refs import mask_opaque_urls
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
async def test_navigate_through_a_payload_ref_redirect_masks_the_landing_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload ref that redirects hands its provenance to the landing URL: the real navigate tool
    derives a ref for it, the engine's context holds the same dict, and the boundary masks it."""
    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)  # no DNS in unit tests
    signed = "https://files.example.test/uploads/deadbeef/resume.pdf?token=eyJhbGciOiJIUzI1NiJ9.c2lnbmVk.QQ"
    landing = "https://cdn.example.test/blob/resume.pdf?X-Amz-Signature=0123456789abcdef0123456789abcdef"

    class _RedirectingPage(_FakePage):
        async def goto(self, url: str, timeout: int | None = None, wait_until: str | None = None) -> None:
            await super().goto(url, timeout, wait_until)
            self.url = landing

    token = mask_opaque_urls({"file": signed}).masked["file"]
    caller = _ScriptedCaller([[("navigate", {"url": token})], [("finish", {"status": "completed", "reason": "done"})]])
    ctx = SkyvernContext(task_id="tsk_redirect")
    skyvern_context.set(ctx)
    try:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_RedirectingPage()),
            llm_caller=caller,
            goal="g",
            parameters={"file": signed},
        )
    finally:
        skyvern_context.reset()
    navigate_message = next(m["content"] for m in caller.message_history if m.get("role") == "tool")
    assert "0123456789abcdef" not in navigate_message and navigate_message.startswith("navigated to opaque_url_")
    assert landing in ctx.opaque_url_refs.values()


@pytest.mark.asyncio
async def test_engine_overwrites_opaque_url_refs_so_a_prior_blocks_refs_never_bleed() -> None:
    """The masking boundary reads ctx.opaque_url_refs, and one SkyvernContext is shared across every
    task block in a workflow run. The engine must OVERWRITE that field with the current task's refs —
    the minted set, or empty when the task mints none — never merge or leave a prior block's stale
    entry. Otherwise a later block masks a URL to a token only the earlier block's resolver can
    reverse, which the model then cannot round-trip back through a tool call."""
    signed = "https://files.example.test/uploads/deadbeef/resume.pdf?token=eyJhbGciOiJIUzI1NiJ9.c2lnbmVk.QQ"
    ctx = SkyvernContext(task_id="tsk_prior")
    ctx.opaque_url_refs = {"opaque_url_stale00": "https://old.example.test/x?token=STALE"}
    skyvern_context.set(ctx)
    try:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
            goal="g",
            parameters={"file": signed},
        )
        # Overwritten with exactly this task's refs; the prior block's stale entry is gone.
        assert ctx.opaque_url_refs == mask_opaque_urls({"file": signed}).refs
        assert "opaque_url_stale00" not in ctx.opaque_url_refs

        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
            goal="g",
            parameters={"first_name": "John"},
        )
        # A task that mints no refs resets the field to empty, not the previous task's refs.
        assert ctx.opaque_url_refs == {}
    finally:
        skyvern_context.reset()


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
    defaults = (DEFAULT_MAX_TURNS, DEFAULT_MAX_TOOL_CALLS, engine_mod.DEFAULT_MAX_TOKENS)
    assert taskv3_runaway_backstops(None) == defaults
    assert taskv3_runaway_backstops(0) == defaults
    # Small cap: the fixed floors dominate, so a productive run keeps its historical headroom.
    assert taskv3_runaway_backstops(10) == defaults
    # Large cap: all guards scale up so the action-step budget -- not the guards -- bounds the run.
    big = 80
    assert taskv3_runaway_backstops(big) == (
        big * MAX_TURNS_PER_ACTION_STEP,
        big * MAX_TOOL_CALLS_PER_ACTION_STEP,
        big * engine_mod.MAX_TOKENS_PER_ACTION_STEP,
    )
    # Monotonic: a larger cap never yields smaller guards.
    t_small, c_small, k_small = taskv3_runaway_backstops(20)
    t_big, c_big, k_big = taskv3_runaway_backstops(80)
    assert t_big >= t_small and c_big >= c_small and k_big >= k_small


def test_runaway_backstops_token_ceiling_anchored_to_the_floor() -> None:
    # The per-step token allowance is anchored so a budget at the action-step floor keeps exactly
    # the historical 1.5M ceiling; only budgets above the floor get a proportionally higher one.
    assert taskv3_runaway_backstops(engine_mod.MIN_ACTION_STEPS)[2] == engine_mod.DEFAULT_MAX_TOKENS
    assert taskv3_runaway_backstops(2 * engine_mod.MIN_ACTION_STEPS)[2] == 2 * engine_mod.DEFAULT_MAX_TOKENS


def test_runaway_backstops_token_ceiling_is_bounded() -> None:
    # The token guard is a runaway BACKSTOP, not a budget: a caller-supplied step cap is not
    # bounded at the route layer, so an extreme value must not carry the token ceiling away
    # with it — the scaling clamps at a hard maximum.
    assert engine_mod.MAX_TOKENS_CEILING == 4 * engine_mod.DEFAULT_MAX_TOKENS
    assert taskv3_runaway_backstops(5000)[2] == engine_mod.MAX_TOKENS_CEILING
    # Turn/tool-call guards keep their pre-existing proportional scaling.
    assert taskv3_runaway_backstops(5000)[0] == 5000 * MAX_TURNS_PER_ACTION_STEP


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
async def test_engine_defers_completion_while_delayed_render_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression for the fixture's delayed-states pattern: a panel's data loads AFTER a delay, and
    # a completion verdict issued mid-render must be deferred until two DOM samples match. The fake
    # page mutates its fingerprint once (the delayed load landing), like the fixture's
    # loading -> loaded transition.
    monkeypatch.setattr("skyvern.forge.taskv3.loop.asyncio.sleep", AsyncMock(return_value=None))
    samples = iter(["loading-shell", "loaded-panel", "loaded-panel", "loaded-panel"])

    async def page_fingerprint() -> str | None:
        return next(samples, "loaded-panel")

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
        page_fingerprint=page_fingerprint,
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
    # Fenced: without a page_fingerprint sampler (the bare-task default) finish(completed) never
    # consults the page, preserving the live bare-task arm's finish path.
    sample_calls = 0

    async def counting_fingerprint() -> str | None:
        nonlocal sample_calls
        sample_calls += 1
        return "fp"

    async def provider() -> Any:
        return object()

    script = [[("finish", {"status": "completed", "reason": "done"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=provider, llm_caller=caller, goal="g", max_action_steps=2, max_turns=4
    )
    assert outcome.status == "completed"
    assert sample_calls == 0


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


@pytest.mark.asyncio
async def test_engine_requests_reasoning_summary_for_bridge_routed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only gpt-5.6 models are routed through litellm's chat->responses bridge, which is the only
    # path that accepts the dict form of reasoning_effort -- so only those calls should carry it.
    from skyvern.forge.taskv3.loop import LoopOutcome
    from skyvern.schemas.llm import LLMConfig

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)

    caller = _ScriptedCaller([])
    caller.uses_openai_responses_bridge = lambda: LLMAPIHandlerFactory.uses_openai_responses_bridge(caller.llm_config)
    caller.llm_config = LLMConfig(
        model_name="gpt-5.6-luna",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
    )
    await run_task_v3_agent_loop(page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="x")

    # The configured effort is preserved verbatim, never hardcoded.
    assert captured["call_kwargs"] == {"reasoning_effort": {"effort": "high", "summary": "auto"}}


@pytest.mark.asyncio
async def test_engine_requests_reasoning_summary_for_gpt56_dispatched_router_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # litellm bridges from the DISPATCHED model string, so a router deployment qualifies only when
    # its litellm model is gpt-5.6-named; an opaque alias must not (litellm would not bridge it).
    from skyvern.forge.taskv3.loop import LoopOutcome
    from skyvern.schemas.llm import LLMRouterConfig, LLMRouterModelConfig

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)

    caller = _ScriptedCaller([])
    caller.uses_openai_responses_bridge = lambda: LLMAPIHandlerFactory.uses_openai_responses_bridge(caller.llm_config)
    caller.llm_config = LLMRouterConfig(
        model_name="azure-gpt-5-6-sol-flex-fallback-router",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(
                model_name="azure-gpt-5-6-sol-flex",
                litellm_params={"model": "azure/gpt-5.6-sol-deployment"},
                model_info={"model_name": "azure/gpt-5.6-sol"},
            ),
        ],
        main_model_group="azure-gpt-5-6-sol-flex",
        reasoning_effort="medium",
    )
    await run_task_v3_agent_loop(page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="x")

    assert captured["call_kwargs"] == {"reasoning_effort": {"effort": "medium", "summary": "auto"}}

    captured.clear()
    caller.llm_config.model_list[0].litellm_params["model"] = "azure/some-opaque-deployment-name"
    await run_task_v3_agent_loop(page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="x")
    assert "reasoning_effort" not in (captured.get("call_kwargs") or {})


@pytest.mark.asyncio
async def test_engine_omits_reasoning_summary_for_non_bridge_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.taskv3.loop import LoopOutcome
    from skyvern.schemas.llm import LLMConfig

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)

    caller = _ScriptedCaller([])
    caller.uses_openai_responses_bridge = lambda: LLMAPIHandlerFactory.uses_openai_responses_bridge(caller.llm_config)
    caller.llm_config = LLMConfig(
        model_name="claude-fable-5",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
    )
    await run_task_v3_agent_loop(page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="x")

    # A plain dict reasoning_effort on a non-bridge model 400s ("Unknown parameter: 'reasoning'"),
    # so this model must never get it -- and no other call kwarg applies here either.
    assert captured["call_kwargs"] is None


@pytest.mark.asyncio
async def test_engine_wires_failure_evidence_gate() -> None:
    # End-to-end wiring: the engine's own ActivityRecency reaches both the loop (which records the
    # solve_captcha attempt) and the finish tool (which holds the failure verdict for one evidence
    # turn). Without either half the first finish(failed) would be accepted immediately.
    async def solve_captcha_handler(args: Any) -> ToolResult:
        return ToolResult.error("a captcha challenge is present but could not be solved this attempt")

    captcha_tool = ToolSpec(
        name="solve_captcha",
        description="solve_captcha",
        parameters={"type": "object", "properties": {}},
        handler=solve_captcha_handler,
        recordable=True,
    )

    async def page_fingerprint() -> str | None:
        return "fp"

    async def provider() -> Any:
        return object()

    script = [
        [("solve_captcha", {})],
        [("finish", {"status": "failed", "reason": "could_not_pass_captcha"})],
        [("finish", {"status": "failed", "reason": "still blocked, re-verified"})],
    ]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=provider,
        llm_caller=caller,
        goal="apply",
        page_fingerprint=page_fingerprint,
        extra_tools=[captcha_tool],
        max_action_steps=4,
        max_turns=8,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "still blocked, re-verified"


@pytest.mark.asyncio
async def test_engine_forwards_the_page_probe_and_withholds_it_from_page_free_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Drop this kwarg anywhere on the way in and the loop classifies every unflagged error as
    # non-poisoning -- the fail-open state -- with every loop-level test still green.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    captured: list[object] = []

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.append(kwargs.get("page_probe"))
        return LoopOutcome(status="completed", reason="ok")

    async def probe() -> str | None:
        return "doc-1"

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x", page_probe=probe
    )
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="x",
        page_probe=probe,
        page_free=True,
    )
    assert captured == [probe, None]


@pytest.mark.asyncio
async def test_engine_forwards_the_page_fingerprint_and_withholds_it_from_page_free_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The innerHTML fingerprint sampler also drives the loop's page-state stall detector (not just
    # make_finish_tool's settle gate) -- drop it anywhere on the way to run_agent_tool_loop and the
    # detector silently loses its rendered-content signal.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    captured: list[object] = []

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.append(kwargs.get("page_fingerprint"))
        return LoopOutcome(status="completed", reason="ok")

    async def fingerprint() -> str | None:
        return "markup-1"

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="x",
        page_fingerprint=fingerprint,
    )
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="x",
        page_fingerprint=fingerprint,
        page_free=True,
    )
    assert captured == [fingerprint, None]


@pytest.mark.asyncio
async def test_engine_wires_the_pending_gate_and_withholds_it_from_page_free_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate needs BOTH halves to reach their destinations and to share one record: the loop writes
    # the clicked control into the watch, the finish tool reads it. Wire either half to a different
    # object, or arm them for a page-free run (which has no page to ask) and disarm them for an
    # ordinary one, and nothing else in the suite would notice.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import SubmitWatch

    finish_args: list[tuple[Any, Any]] = []
    loop_watches: list[Any] = []
    real_make = engine_mod.make_finish_tool
    real_loop = engine_mod.run_agent_tool_loop

    def capturing_make(*args: Any, **kwargs: Any) -> Any:
        finish_args.append((kwargs.get("pending_marker"), kwargs.get("submit_watch")))
        return real_make(*args, **kwargs)

    async def capturing_loop(**kwargs: Any) -> LoopOutcome:
        loop_watches.append(kwargs.get("submit_watch"))
        return await real_loop(**kwargs)

    monkeypatch.setattr(engine_mod, "make_finish_tool", capturing_make)
    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", capturing_loop)

    async def provider() -> Any:
        return object()

    async def pending_marker(selector: str) -> str | None:
        return "the submit control still reads 'Submitting…'"

    script = [[("finish", {"status": "completed", "reason": "done"})]]
    await run_task_v3_agent_loop(
        page_provider=provider,
        llm_caller=_ScriptedCaller(script),
        goal="apply",
        pending_marker=pending_marker,
        max_action_steps=2,
        max_turns=4,
    )
    await run_task_v3_agent_loop(
        page_provider=provider,
        llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "criteria hold"})]]),
        goal="assess",
        page_free=True,
        pending_marker=pending_marker,
        max_action_steps=2,
        max_turns=4,
    )
    assert [marker for marker, _watch in finish_args] == [pending_marker, None], finish_args
    watches = [watch for _marker, watch in finish_args]
    assert isinstance(watches[0], SubmitWatch), watches
    assert watches[1] is None, watches
    assert loop_watches[0] is watches[0], (loop_watches, watches)
    assert loop_watches[1] is None, loop_watches


@pytest.mark.asyncio
async def test_engine_shares_one_semantic_commit_stats_between_the_browser_tools_and_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The counter needs BOTH halves on ONE record: the browser tools increment it, the loop's
    # terminal record reads it. Wire either half to a different object and every production run
    # reports 0 opportunities / 0 accepts forever, and nothing else in the suite would notice.
    tool_stats: list[Any] = []
    loop_stats: list[Any] = []
    real_build = engine_mod.build_browser_tools
    real_loop = engine_mod.run_agent_tool_loop

    def capturing_build(*args: Any, **kwargs: Any) -> Any:
        tool_stats.append(kwargs.get("semantic_commit_stats"))
        return real_build(*args, **kwargs)

    async def capturing_loop(**kwargs: Any) -> LoopOutcome:
        loop_stats.append(kwargs.get("semantic_commit_stats"))
        return await real_loop(**kwargs)

    monkeypatch.setattr(engine_mod, "build_browser_tools", capturing_build)
    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", capturing_loop)

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
        goal="apply",
        max_action_steps=2,
        max_turns=4,
    )
    assert isinstance(tool_stats[0], SemanticCommitStats), tool_stats
    assert loop_stats[0] is tool_stats[0], (loop_stats, tool_stats)


@pytest.mark.asyncio
async def test_engine_omits_semantic_commit_stats_when_the_verify_kill_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tier only ever increments under TASK_V3_SEMANTIC_COMMIT_VERIFY, so with the kill switch
    # off a live object makes every page-ful run emit 0 opportunities for a tier that is turned
    # off — the denominator drag omission exists to prevent. The switch and the omission contract
    # have to agree, or flipping the switch silently corrupts the accept rate instead of ending it.
    monkeypatch.setattr(settings, "TASK_V3_SEMANTIC_COMMIT_VERIFY", False)
    loop_stats: list[Any] = []
    real_loop = engine_mod.run_agent_tool_loop

    async def capturing_loop(**kwargs: Any) -> LoopOutcome:
        loop_stats.append(kwargs.get("semantic_commit_stats"))
        return await real_loop(**kwargs)

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", capturing_loop)
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
        goal="apply",
        max_action_steps=2,
        max_turns=4,
    )
    assert loop_stats == [None], loop_stats


@pytest.mark.asyncio
async def test_signed_payload_url_reaches_model_only_as_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # A presigned file URL reaches the model only as its opaque_url_ token in the payload; the tools
    # resolve that token to the untouched bytes (navigate's goto, file_upload's download), and the
    # finish output is un-masked so the customer never sees the token.
    segment = ("0123456789abcdef" * 3)[:40]
    host_and_path = f"https://files.example.test/uploads/{segment}/resume.pdf"
    credential_value = "AKIAEXAMPLE0123456%2F20260824%2Fus-east-1%2Fs3%2Faws4_request"
    signature = "f1e2d3c4b5a697887766554433221100aabbccddeeff00112233445566778899"
    signed_url = (
        f"{host_and_path}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={credential_value}"
        f"&X-Amz-Date=20260824T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature={signature}"
    )
    plain_url = "https://portfolio.example.test/jo"
    parameters = {"first_name": "Jo", "resume_url": signed_url, "portfolio_url": plain_url}
    token = next(iter(mask_opaque_urls(parameters).refs))

    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)

    captured_source: dict[str, str] = {}

    async def fake_download_file(source: str, output_dir: str | None = None, organization_id: str | None = None) -> str:
        captured_source["source"] = source
        request_info = aiohttp.RequestInfo(
            url=yarl.URL(signed_url), method="GET", headers={}, real_url=yarl.URL(signed_url)
        )
        raise aiohttp.ClientResponseError(request_info=request_info, history=(), status=400, message="Bad Request")

    import skyvern.forge.sdk.api.files as files_module

    monkeypatch.setattr(files_module, "download_file", fake_download_file)

    script = [
        [("navigate", {"url": token})],
        [("file_upload", {"selector": "#cv", "file": token})],
        [
            (
                "finish",
                {
                    "status": "failed",
                    "reason": f"upload of {token} was rejected",
                    "extracted_output": {"uploaded": [token], "note": f"used {token}"},
                },
            )
        ],
    ]
    caller = _ScriptedCaller(script)
    page = _FakePage()
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(page),
            llm_caller=caller,
            goal="Apply to the role and upload the attached resume.",
            parameters=parameters,
        )
        assert ctx.runtime_secret_values == set()  # the customer's own URL is never enrolled in redaction
    finally:
        skyvern_context.reset()

    assert captured_source["source"] == signed_url  # the real bytes reached the download call
    assert page.url == signed_url  # navigate resolved the token before goto

    user_prompt = next(m["content"] for m in outcome.messages if m.get("role") == "user")
    assert signed_url not in user_prompt
    assert signature not in user_prompt and credential_value not in user_prompt
    assert token in user_prompt
    assert plain_url in user_prompt  # nosemgrep: incomplete-url-substring-sanitization

    assert outcome.status == "failed"
    assert signed_url in outcome.reason  # nosemgrep: incomplete-url-substring-sanitization
    assert token not in outcome.reason
    assert outcome.extracted_output == {"uploaded": [signed_url], "note": f"used {signed_url}"}


@pytest.mark.asyncio
async def test_business_identifier_value_under_a_non_signing_key_stays_readable() -> None:
    # A token-shaped VALUE under an ordinary business KEY (order id, not a signing param) must never be
    # tokenized: neither in the payload nor in ordinary page content the model reads.
    order_id = "ORD2026AUG24X7Q1A"
    order_url = f"https://shop.example.test/orders?orderId={order_id}"
    parameters = {"order_url": order_url}

    class _OrderPage(_FakePage):
        async def content(self) -> str:
            return f"<html><body>Order {order_id} shipped</body></html>"

    script = [
        [("get_html", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_OrderPage()),
        llm_caller=caller,
        goal="check order status",
        parameters=parameters,
    )
    user_message = next(m for m in outcome.messages if m.get("role") == "user")["content"]
    assert order_url in user_message  # nosemgrep: incomplete-url-substring-sanitization
    assert "opaque_url_" not in user_message

    tool_messages = {m["name"]: m["content"] for m in outcome.messages if m.get("role") == "tool"}
    assert f"Order {order_id} shipped" in tool_messages["get_html"]


@pytest.mark.asyncio
async def test_hash_route_job_url_is_not_masked() -> None:
    # A SPA hash-route job URL is an ordinary payload value, not a signed URL: it must reach the
    # model verbatim in the user prompt, not as an opaque_url_ token.
    job_url = "https://careers.example.test/#/jobs/software-engineer-2026"
    parameters = {"job_url": job_url}
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])
    ctx = SkyvernContext(task_id="tsk_2")
    skyvern_context.set(ctx)
    try:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=caller,
            goal="Apply using the given job URL.",
            parameters=parameters,
        )
    finally:
        skyvern_context.reset()

    transcript = json.dumps(outcome.messages)
    assert job_url in transcript  # nosemgrep: incomplete-url-substring-sanitization
    assert "opaque_url_" not in transcript


@pytest.mark.asyncio
async def test_page_free_mode_does_not_mask_signed_urls() -> None:
    # Page-free mode has no tools to resolve an opaque_url_ token, so the payload stays verbatim.
    signed_url = "https://files.example.test/uploads/x?token=eyJhbGciOiJIUzI1NiJ9c2lnbmVkQ29ycmVjdEhvcnNl"
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "criteria hold"})]])
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),  # never consulted: page_free has no tools
        llm_caller=caller,
        goal="assess",
        page_free=True,
        parameters={"u": signed_url},
        max_turns=4,
    )
    user_message = next(m for m in outcome.messages if m.get("role") == "user")["content"]
    assert (
        signed_url in user_message and "opaque_url_" not in user_message
    )  # nosemgrep: incomplete-url-substring-sanitization


@pytest.mark.asyncio
async def test_engine_forwards_completion_hooks_and_gates_guidance_on_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # completion_probe must reach the loop and completion_blocker must reach the finish tool, and
    # the download-completion guidance is appended to the system prompt only when a probe is given.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.engine import DOWNLOAD_COMPLETION_GUIDANCE
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}
    finish_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    real_make_finish_tool = engine_mod.make_finish_tool

    def capturing_make_finish_tool(*args: Any, **kwargs: Any) -> Any:
        finish_kwargs.update(kwargs)
        return real_make_finish_tool(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    monkeypatch.setattr(engine_mod, "make_finish_tool", capturing_make_finish_tool)

    async def probe(_staged: frozenset[str]) -> str | None:
        return "a file finished downloading"

    async def blocker(_staged: frozenset[str]) -> str | None:
        return None

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="download the file",
        completion_probe=probe,
        completion_blocker=blocker,
    )
    assert loop_kwargs["completion_probe"] is probe
    assert finish_kwargs["completion_blocker"] is blocker
    assert DOWNLOAD_COMPLETION_GUIDANCE in loop_kwargs["system_prompt"]
    # One staged_downloads set is shared between the loop and the finish tool -- a name staged via
    # a billable tool call must be visible to the SAME finish-tool blocker, not a divergent copy.
    assert loop_kwargs["staged_downloads"] is finish_kwargs["staged_downloads"]
    assert isinstance(loop_kwargs["staged_downloads"], set)

    loop_kwargs.clear()
    finish_kwargs.clear()
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="download the file"
    )
    assert loop_kwargs["completion_probe"] is None
    assert finish_kwargs["completion_blocker"] is None
    assert DOWNLOAD_COMPLETION_GUIDANCE not in loop_kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_engine_guidance_keyed_on_which_hooks_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # blocker-only (extraction tasks: the probe would end the loop before the model returns
    # extracted_output) gets the DOWNLOAD_REQUIRED variant naming finish(completed) as the model's
    # own job; probe-only (download_timeout alone, wait-only) has no completion semantics at all,
    # so it gets neither guidance string.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.engine import DOWNLOAD_COMPLETION_GUIDANCE, DOWNLOAD_REQUIRED_GUIDANCE
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)

    async def blocker(_staged: frozenset[str]) -> str | None:
        return None

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="download the file",
        completion_blocker=blocker,
    )
    assert DOWNLOAD_REQUIRED_GUIDANCE in loop_kwargs["system_prompt"]
    assert DOWNLOAD_COMPLETION_GUIDANCE not in loop_kwargs["system_prompt"]

    loop_kwargs.clear()

    async def probe(_staged: frozenset[str]) -> str | None:
        return None

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="download the file",
        completion_probe=probe,
    )
    assert DOWNLOAD_REQUIRED_GUIDANCE not in loop_kwargs["system_prompt"]
    assert DOWNLOAD_COMPLETION_GUIDANCE not in loop_kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_engine_drops_download_hooks_in_page_free_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # No browser tools exist to trigger a download, so a blocker would refuse finish(completed)
    # forever; page-free runs get neither hook nor the download guidance.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.engine import DOWNLOAD_COMPLETION_GUIDANCE, DOWNLOAD_REQUIRED_GUIDANCE
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}
    finish_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    real_make_finish_tool = engine_mod.make_finish_tool

    def capturing_make_finish_tool(*args: Any, **kwargs: Any) -> Any:
        finish_kwargs.update(kwargs)
        return real_make_finish_tool(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    monkeypatch.setattr(engine_mod, "make_finish_tool", capturing_make_finish_tool)

    async def probe(_staged: frozenset[str]) -> str | None:
        return "a file finished downloading"

    async def blocker(_staged: frozenset[str]) -> str | None:
        return "no download yet"

    async def verification_blocker() -> str | None:
        return "no code arrived"

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="validate the record",
        page_free=True,
        completion_probe=probe,
        completion_blocker=blocker,
        verification_blocker=verification_blocker,
    )
    assert loop_kwargs["completion_probe"] is None
    assert finish_kwargs["completion_blocker"] is None
    assert finish_kwargs["verification_blocker"] is None
    assert DOWNLOAD_COMPLETION_GUIDANCE not in loop_kwargs["system_prompt"]
    assert DOWNLOAD_REQUIRED_GUIDANCE not in loop_kwargs["system_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("page_free", [False, True])
async def test_engine_system_prompt_carries_the_current_date(monkeypatch: pytest.MonkeyPatch, page_free: bool) -> None:
    # A relative-date goal ("two weeks from today") is unanswerable without a reference date; the
    # model was observed typing past dates. The date is computed at assembly, never hardcoded.
    from datetime import UTC, datetime

    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    before = datetime.now(UTC)
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="fill in the date available: two weeks from today",
        page_free=page_free,
    )
    after = datetime.now(UTC)
    system_prompt = loop_kwargs["system_prompt"]
    assert any(
        f"Today's date is {d.strftime('%Y-%m-%d')} ({d.strftime('%A')})" in system_prompt for d in (before, after)
    ), system_prompt[-300:]


@pytest.mark.asyncio
async def test_engine_system_prompt_dates_in_the_runs_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    # The browser runs in the proxy's timezone (browser_factory sets ctx.tz_info); the stated date
    # must be that zone's today, or UTC drifts a day ahead of/behind the page every evening.
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    # One of the two extreme zones is always on a different calendar day than UTC.
    tz = next(
        z
        for z in (ZoneInfo("Pacific/Kiritimati"), ZoneInfo("Etc/GMT+12"))
        if datetime.now(z).date() != datetime.now(UTC).date()
    )
    ctx = SkyvernContext(task_id="tsk_tz")
    ctx.tz_info = tz
    skyvern_context.set(ctx)
    try:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([]),
            goal="fill in the date available: two weeks from today",
        )
    finally:
        skyvern_context.reset()
    system_prompt = loop_kwargs["system_prompt"]
    assert f"Today's date is {datetime.now(tz).strftime('%Y-%m-%d')}" in system_prompt, system_prompt[-200:]
    assert f"Today's date is {datetime.now(UTC).strftime('%Y-%m-%d')}" not in system_prompt


@pytest.mark.asyncio
async def test_engine_drops_a_leftover_refresh_signal_when_the_loop_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The run context outlives the run: a signal raised as the loop was cancelled must not fire on
    # the next block's first action.
    async def _cancelled_loop(*args: Any, **kwargs: Any) -> Any:
        skyvern_context.current().refresh_working_page = True
        raise asyncio.CancelledError()

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _cancelled_loop)
    ctx = SkyvernContext(task_id="tsk_refresh_cancelled")
    skyvern_context.set(ctx)
    try:
        with pytest.raises(asyncio.CancelledError):
            await run_task_v3_agent_loop(
                page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="noop"
            )
    finally:
        skyvern_context.reset()
    assert ctx.refresh_working_page is False


def test_caller_level_bridge_check_denies_raw_client_dispatch() -> None:
    # A custom/BYO model can be NAMED gpt-5.6-* yet dispatch through the raw OpenAI client branch,
    # which never bridges — the dict form there is a schema violation on every call. The caller-level
    # check must deny on dispatch path, whatever the name says.
    from types import SimpleNamespace

    from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
    from skyvern.schemas.llm import LLMConfig

    config = LLMConfig(
        model_name="openrouter/gpt-5.6-custom",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
    )
    raw_client_self = SimpleNamespace(
        openai_client=object(), _custom_openrouter=False, llm_config=config, original_llm_key="X"
    )
    assert LLMCaller.uses_openai_responses_bridge(raw_client_self) is False
    openrouter_self = SimpleNamespace(
        openai_client=None, _custom_openrouter=True, llm_config=config, original_llm_key="X"
    )
    assert LLMCaller.uses_openai_responses_bridge(openrouter_self) is False
    litellm_self = SimpleNamespace(
        openai_client=None, _custom_openrouter=False, llm_config=config, original_llm_key="X"
    )
    assert LLMCaller.uses_openai_responses_bridge(litellm_self) is True


def test_engine_omits_summary_when_caller_denies_the_bridge() -> None:
    from skyvern.schemas.llm import LLMConfig

    caller = _ScriptedCaller([])
    caller.uses_openai_responses_bridge = lambda: False
    caller.llm_config = LLMConfig(
        model_name="gpt-5.6-luna",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
    )
    assert engine_mod._reasoning_effort_with_summary(caller) is None


def test_bridge_check_mirrors_litellm_dispatched_name_only() -> None:
    # litellm decides the bridge from the DISPATCHED model string alone. A model_info label saying
    # gpt-5.6 must NOT flip the gate for an opaque deployment alias: litellm would not bridge that
    # call, and the dict form would reach a chat-completions endpoint.
    from skyvern.schemas.llm import LiteLLMParams, LLMConfig

    aliased = LLMConfig(
        model_name="azure/opaque-deployment-alias",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
        litellm_params=LiteLLMParams(model_info={"model_name": "azure/gpt-5.6-sol"}),
    )
    assert LLMAPIHandlerFactory.uses_openai_responses_bridge(aliased) is False
    dispatched_named = LLMConfig(
        model_name="azure/gpt-5.6-sol-deployment",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
    )
    assert LLMAPIHandlerFactory.uses_openai_responses_bridge(dispatched_named) is True


def test_bridge_check_denies_router_with_non_bridge_fallback_group() -> None:
    # A mixed router could hand the dict reasoning_effort to a non-bridge fallback deployment,
    # which rejects it — every serving deployment must qualify.
    from skyvern.schemas.llm import LLMRouterConfig, LLMRouterModelConfig

    config = LLMRouterConfig(
        model_name="mixed-router",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(
                model_name="primary-group",
                litellm_params={"model": "gpt-5.6-luna"},
                model_info={"model_name": "gpt-5.6-luna"},
            ),
            LLMRouterModelConfig(
                model_name="fallback-group",
                litellm_params={"model": "gpt-4.1"},
                model_info={"model_name": "gpt-4.1"},
            ),
        ],
        main_model_group="primary-group",
        fallback_model_group="fallback-group",
        reasoning_effort="high",
    )
    assert LLMAPIHandlerFactory.uses_openai_responses_bridge(config) is False


def test_caller_level_bridge_check_denies_custom_llm_keys() -> None:
    # A custom/BYO key routes litellm at an arbitrary api_base that need not implement
    # /v1/responses, whatever the user named the model — default-deny.
    from types import SimpleNamespace

    from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
    from skyvern.forge.sdk.api.llm.custom_llm_registry import CUSTOM_LLM_KEY_PREFIX
    from skyvern.schemas.llm import LLMConfig

    config = LLMConfig(
        model_name="openai/gpt-5.6-byo",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
    )
    custom_self = SimpleNamespace(
        openai_client=None,
        _custom_openrouter=False,
        llm_config=config,
        original_llm_key=f"{CUSTOM_LLM_KEY_PREFIX}abc123",
    )
    assert LLMCaller.uses_openai_responses_bridge(custom_self) is False


def test_caller_level_bridge_check_denies_openai_provider_with_custom_api_base() -> None:
    # The built-in OPENAI_COMPATIBLE registration (configurable key name) dispatches openai/<model>
    # at an arbitrary api_base that need not implement /v1/responses — deny. Azure legitimately
    # carries an api_base and does bridge.
    from types import SimpleNamespace

    from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
    from skyvern.schemas.llm import LiteLLMParams, LLMConfig

    compat = LLMConfig(
        model_name="openai/gpt-5.6-selfhosted",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
        litellm_params=LiteLLMParams(api_base="https://byo.example.internal/v1"),
    )
    compat_self = SimpleNamespace(
        openai_client=None, _custom_openrouter=False, llm_config=compat, original_llm_key="OPENAI_COMPATIBLE"
    )
    assert LLMCaller.uses_openai_responses_bridge(compat_self) is False

    azure = LLMConfig(
        model_name="azure/gpt-5.6-sol-deployment",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
        litellm_params=LiteLLMParams(api_base="https://example.openai.azure.com"),
    )
    azure_self = SimpleNamespace(
        openai_client=None, _custom_openrouter=False, llm_config=azure, original_llm_key="AZURE_OPENAI_GPT5_6_SOL"
    )
    assert LLMCaller.uses_openai_responses_bridge(azure_self) is True


@pytest.mark.asyncio
async def test_terminal_log_carries_duration_and_block_type() -> None:
    # The v1-vs-v3 wall-time dashboard reads this log line; it needs the loop's own wall-clock and
    # the block context to slice workflow-block runs (SKY-15499).
    script = [[("finish", {"status": "completed", "reason": "ok"})]]
    with capture_logs() as logs:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller(script),
            goal="x",
            block_type="navigation",
        )
    terminal = [e for e in logs if e.get("event") == "taskv3 engine loop finished"]
    assert len(terminal) == 1
    assert terminal[0]["block_type"] == "navigation"
    assert isinstance(terminal[0]["duration_seconds"], float)
    assert terminal[0]["duration_seconds"] >= 0.0

    with capture_logs() as logs:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller(script),
            goal="x",
        )
    terminal = [e for e in logs if e.get("event") == "taskv3 engine loop finished"]
    assert terminal[0]["block_type"] is None  # bare task: no block context


@pytest.mark.asyncio
async def test_terminal_log_carries_loop_telemetry_and_the_two_terminal_records_stay_dead() -> None:
    # The only test that can catch the loop telemetry silently dropping off this line, or either
    # per-run record it replaced coming back — everything else asserts the payload, not the join.
    script = [[("finish", {"status": "completed", "reason": "ok"})]]
    with capture_logs() as logs:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller(script),
            goal="x",
        )

    assert not [e for e in logs if e.get("event") == "taskv3 loop progress ledger final"]
    assert not [e for e in logs if e.get("event") == "taskv3 canonical progress final"]

    terminal = [e for e in logs if e.get("event") == "taskv3 engine loop finished"]
    assert len(terminal) == 1, terminal
    record = terminal[0]
    assert record["form_ever_armed"] is False
    for member in _ProgressEvidence:
        assert f"clear_{member.value}" in record, record
    assert record["status"] == "completed"
    assert isinstance(record["turns"], int)
    assert "outcome_status" not in record, record


@pytest.mark.asyncio
async def test_terminal_log_merges_a_fully_populated_telemetry_without_colliding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sibling test above drives a bare run, so it merges an almost-empty payload — it would stay
    # green if a ledger or semantic-commit key ever collided with one of this line's fixed kwargs,
    # and that collision is a TypeError inside LOG.info that would take the whole run's record with
    # it. This one hands the engine the maximal payload every optional branch can produce.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LedgerTerminalFields, TerminalTelemetry

    stats = SemanticCommitStats(opportunities=3, accepts=2)
    telemetry = TerminalTelemetry(
        form_ever_armed=True,
        survival={
            "peak_same_touches": 5,
            "peak_same_errors": 2,
            "looping_targets": 1,
            **{f"clear_{member.value}": 1 for member in _ProgressEvidence},
        },
        ledger=LedgerTerminalFields(
            peak_actions_since_progress=9,
            actions_since_progress=4,
            form_armed=False,
            would_fire=True,
        ),
        peak_page_state_stall_rounds=6,
        peak_probe_revisits=7,
        semantic_commit=stats,
    )

    async def _loaded(**kwargs: object) -> LoopOutcome:
        outcome = LoopOutcome(status="completed", reason="ok")
        outcome.telemetry = telemetry
        return outcome

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _loaded)
    with capture_logs() as logs:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x"
        )

    terminal = [e for e in logs if e.get("event") == "taskv3 engine loop finished"]
    assert len(terminal) == 1, terminal
    record = terminal[0]
    # Every key the payload can carry survives the merge, and the line's own kwargs are untouched.
    for key, value in telemetry.log_fields().items():
        assert record[key] == value, (key, record)
    assert record["status"] == "completed"
    assert record["block_type"] is None
    # form_armed and form_ever_armed now ride the same record and mean different things.
    assert record["form_armed"] is False
    assert record["form_ever_armed"] is True


@pytest.mark.asyncio
async def test_engine_forwards_the_error_code_mapping_to_the_finish_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # The engine hop is the one link no other test covers: the agent-side wiring test mocks the loop,
    # the loop tests call make_finish_tool directly, and the agent tests hand-build a LoopOutcome. Drop
    # this pass-through and the codes silently stop reaching the model in production.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    finish_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        return LoopOutcome(status="completed", reason="ok")

    real_make_finish_tool = engine_mod.make_finish_tool

    def capturing_make_finish_tool(*args: Any, **kwargs: Any) -> Any:
        finish_kwargs.update(kwargs)
        return real_make_finish_tool(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    monkeypatch.setattr(engine_mod, "make_finish_tool", capturing_make_finish_tool)

    mapping = {"COVERAGE_NOT_ACTIVE": "The member has no active plan today."}
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="check coverage",
        error_code_mapping=mapping,
    )
    assert finish_kwargs["error_code_mapping"] == mapping
