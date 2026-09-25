"""Unit tests for the native Task V3 engine (prompt + tools + loop assembly).

Reuses the scripted fake LLMCaller from the loop test and the fake Playwright page
from the tools test, so the engine's wiring is exercised without a real LLM or browser.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import litellm
import pytest
import yarl
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    LLM_RETRY_CHAIN_EXHAUSTED_MESSAGE,
    LLMAPIHandlerFactory,
    LLMCaller,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.context_manager import RANDOM_SECRET_ID_PREFIX
from skyvern.forge.taskv3 import engine as engine_mod
from skyvern.forge.taskv3 import loop as loop_mod
from skyvern.forge.taskv3.engine import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    MAX_TOOL_CALLS_PER_ACTION_STEP,
    MAX_TURNS_PER_ACTION_STEP,
    OPAQUE_URL_GUIDANCE,
    REQUIRED_FIELD_ANSWERS_ANCHOR,
    SELF_SCREEN_ANCHOR,
    SYSTEM_PROMPT,
    UNANSWERABLE_FIELD_REMEDY_CONTROL,
    UNANSWERABLE_FIELD_REMEDY_PROMPT,
    UNANSWERABLE_FIELD_REMEDY_TREATMENT,
    coerce_v3_parameters,
    run_task_v3_agent_loop,
    system_prompt_for_run_arms,
    taskv3_runaway_backstops,
)
from skyvern.forge.taskv3.goal_check import INSTRUCTIONS_MAX_CHARS
from skyvern.forge.taskv3.llm_call_params import reasoning_effort_with_summary
from skyvern.forge.taskv3.loop import (
    CODE_TOOL_NAME,
    NAV_DEAD_END_GUARD,
    LoopOutcome,
    SemanticCommitStats,
    ToolResult,
    ToolSpec,
    _ProgressEvidence,
)
from skyvern.forge.taskv3.opaque_refs import OpaqueUrlRefs, mask_opaque_urls
from skyvern.forge.taskv3.run_arms import REQUIRED_FIELD_ANSWERS_FLAG, UNANSWERABLE_FIELD_REMEDY_FLAG
from skyvern.forge.taskv3.tools import PAGE_UNAVAILABLE_ERROR
from skyvern.schemas.llm import LLMConfig, LLMRouterConfig, LLMRouterModelConfig
from tests.unit.helpers import fallback_receipts
from tests.unit.scoped_asyncio import ScopedAsyncio
from tests.unit.test_taskv3_loop import _ScriptedCaller
from tests.unit.test_taskv3_tools import (
    _SURFACE_OFF_TOOL_NAMES,
    _DownloadFakePage,
    _FakePage,
    _fixed_page_provider,
)


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

    async def fake_download_file(source: str, output_dir: str | None = None, **kwargs: object) -> str:
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
async def test_signed_url_rendered_into_model_facing_text_reaches_model_only_as_a_resolvable_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A workflow template renders a file parameter into the goal, the system guidance and the block URL,
    # with no payload carrying it. Each must get the same treatment as the payload: token in, real URL out.
    signature = "f1e2d3c4b5a697887766554433221100aabbccddeeff00112233445566778899"
    cover_signature = "99887766554433221100ffeeddccbbaa00112233445566778899aabbccddeeff"
    query = "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20260824T000000Z&X-Amz-Expires=2700&X-Amz-SignedHeaders=host"
    signed_url = f"https://files.example.test/uploads/0123456789abcdef/resume.pdf{query}&X-Amz-Signature={signature}"
    cover_url = (
        f"https://files.example.test/uploads/0123456789abcdef/cover.pdf{query}&X-Amz-Signature={cover_signature}"
    )
    start_signature = "00112233445566778899aabbccddeeff99887766554433221100ffeeddccbbaa"
    start_url = f"https://files.example.test/uploads/O'Brien/posting.pdf{query}&X-Amz-Signature={start_signature}"
    plain_url = "https://portfolio.example.test/jo"
    goal = f"Fill out the application.\n\nresume: {signed_url}\nportfolio: {plain_url}.\n"
    # Distinct URLs, so each token can only resolve through the refs minted from its own text.
    token = OpaqueUrlRefs(masked=None, refs={}).mint_in_text(signed_url)
    cover_token = OpaqueUrlRefs(masked=None, refs={}).mint_in_text(cover_url)
    # The apostrophe is a legal path character that ends a prose URL match.
    start_token = OpaqueUrlRefs(masked=None, refs={}).derive(start_url)
    assert len({token, cover_token, start_token}) == 3 and cover_token.startswith("opaque_url_")

    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)
    captured_sources: list[str] = []

    async def fake_download_file(source: str, output_dir: str | None = None, **kwargs: object) -> str:
        captured_sources.append(source)
        request_info = aiohttp.RequestInfo(url=yarl.URL(source), method="GET", headers={}, real_url=yarl.URL(source))
        raise aiohttp.ClientResponseError(request_info=request_info, history=(), status=400, message="Bad Request")

    import skyvern.forge.sdk.api.files as files_module

    monkeypatch.setattr(files_module, "download_file", fake_download_file)

    caller = _ScriptedCaller(
        [
            [("file_upload", {"selector": "#cv", "file": token})],
            [("file_upload", {"selector": "#cover", "file": cover_token})],
            [("navigate", {"url": start_token})],
            [("finish", {"status": "failed", "reason": "upload rejected"})],
        ]
    )
    page = _FakePage()
    skyvern_context.set(SkyvernContext(task_id="tsk_goal"))
    try:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(page),
            llm_caller=caller,
            goal=goal,
            parameters=None,
            starting_url=start_url,
            extra_system_guidance=f"Always attach the cover letter at {cover_url}",
        )
    finally:
        skyvern_context.reset()

    user_prompt = next(m["content"] for m in outcome.messages if m.get("role") == "user")
    assert f"resume: {token}\n" in user_prompt
    assert f"You start on: {start_token}" in user_prompt
    assert f"portfolio: {plain_url}." in user_prompt  # nosemgrep: incomplete-url-substring-sanitization
    system_prompt = next(m["content"] for m in outcome.messages if m.get("role") == "system")
    assert f"Always attach the cover letter at {cover_token}" in system_prompt
    assert OPAQUE_URL_GUIDANCE in system_prompt
    transcript = json.dumps(outcome.messages)
    assert all(sig not in transcript for sig in (signature, cover_signature, start_signature))
    assert captured_sources == [signed_url, cover_url]
    assert page.url == start_url


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
    # Page-free mode has no tools to resolve an opaque_url_ token, so every model-facing URL stays verbatim.
    signed_url = "https://files.example.test/uploads/x?token=eyJhbGciOiJIUzI1NiJ9c2lnbmVkQ29ycmVjdEhvcnNl"
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "criteria hold"})]])
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),  # never consulted: page_free has no tools
        llm_caller=caller,
        goal=f"assess {signed_url}",
        starting_url=signed_url,
        page_free=True,
        parameters={"u": signed_url},
        max_turns=4,
    )
    user_message = next(m for m in outcome.messages if m.get("role") == "user")["content"]
    assert f"assess {signed_url}" in user_message  # nosemgrep: incomplete-url-substring-sanitization
    assert "opaque_url_" not in user_message


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

    async def verification_blocker(_status: str) -> str | None:
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
    assert reasoning_effort_with_summary(caller) is None


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
async def test_terminal_log_carries_the_guard_class_that_ended_the_run() -> None:
    # The class a guard verdict used to prefix onto the customer-facing reason lives here now, one row
    # per run (SKY-16271): a dashboard counting how often a policy ends a run reads this field, so it
    # has to survive the trip out of the loop.
    with capture_logs() as logs:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "never runs"})]]),
            goal="x",
            initial_navigation_status=404,
        )
    terminal = [e for e in logs if e.get("event") == "taskv3 engine loop finished"]
    assert outcome.status == "terminated"
    assert terminal[0]["status"] == "terminated"
    assert terminal[0]["guard"] == NAV_DEAD_END_GUARD

    # A model-authored verdict carries no guard, so the field partitions cleanly.
    with capture_logs() as logs:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "ok"})]]),
            goal="x",
        )
    terminal = [e for e in logs if e.get("event") == "taskv3 engine loop finished"]
    assert terminal[0]["guard"] is None


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
@pytest.mark.parametrize(
    ("block_type", "refused_count"), [("extraction", 1), ("task", 0), ("navigation", 0), (None, 0)]
)
async def test_entry_is_refused_only_in_an_extraction_block(block_type: str | None, refused_count: int) -> None:
    script = [
        [("type", {"selector": "#q", "text": "Jane Doe"})],
        [("finish", {"status": "completed", "reason": "ok"})],
    ]
    with capture_logs() as logs:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller(script),
            goal="x",
            block_type=block_type,
        )
    refused = [e for e in logs if e.get("event") == "taskv3 loop extraction entry refused"]
    assert len(refused) == refused_count


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


def _advertised(caller: _ScriptedCaller) -> set[str]:
    """The tool names the MODEL was actually offered, read off the request the caller built."""
    return {t["function"]["name"] for t in (caller.sent_tools or [])}


def _stub_code_tool() -> ToolSpec:
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult.ok("{}")

    return ToolSpec(CODE_TOOL_NAME, "run python", {"type": "object", "properties": {}}, handler)


async def _run_to_finish(caller: _ScriptedCaller, *, frame_perception: bool | None = None) -> None:
    # A real run always carries a context with a run identity; the code tool is withheld without one,
    # so a context-free call would exercise the identity gate rather than the surface under test.
    context = SkyvernContext(task_id="tsk_surface")
    if frame_perception is not None:
        context.frame_perception_flag = frame_perception
        context.frame_perception_resolved_run_id = context.task_id
    skyvern_context.set(context)
    try:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=caller,
            goal="Do the thing.",
            starting_url="https://example.test/",
        )
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_code_tool_surface_off_never_asks_the_deployment_for_a_code_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = False

    async def _build(**kwargs: Any) -> ToolSpec | None:
        nonlocal asked
        asked = True
        return _stub_code_tool()

    monkeypatch.setattr(app.AGENT_FUNCTION, "build_task_v3_code_tool", _build)
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])
    await _run_to_finish(caller)

    assert not asked
    assert CODE_TOOL_NAME not in _advertised(caller)


@pytest.mark.asyncio
async def test_code_tool_surface_add_and_replace_advertise_exact_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _build(**kwargs: Any) -> ToolSpec | None:
        return _stub_code_tool()

    monkeypatch.setattr(app.AGENT_FUNCTION, "build_task_v3_code_tool", _build)
    monkeypatch.setattr(settings, "TASK_V3_CODE_TOOL_SURFACE", "add")
    add_caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])
    await _run_to_finish(add_caller)

    monkeypatch.setattr(settings, "TASK_V3_CODE_TOOL_SURFACE", "replace")
    replace_caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])
    await _run_to_finish(replace_caller)

    # `add` keeps every action tool and gains the code tool; `replace` keeps only perception and
    # waiting. `finish` is assembled after this filter and survives both, which is what makes a
    # `replace` run able to end at all.
    assert _advertised(add_caller) == _SURFACE_OFF_TOOL_NAMES | {CODE_TOOL_NAME, "finish"}
    assert _advertised(replace_caller) == {"observe", "get_html", "look", "wait", CODE_TOOL_NAME, "finish"}


@pytest.mark.asyncio
async def test_frame_perception_withholds_the_code_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """G3: the code tool and frame perception do not run together.

    Code driving the page directly never reaches the wrapper that writes the realm-attributed
    ledger, so in-frame fills and submits would be invisible to the data-loss guard and the
    completion gate. Asserted at the advertised-tool set, not at the gate, because what matters is
    that the model is never offered the tool -- a gate that runs and then leaks is still a leak.
    """
    asked = False

    async def _build(**kwargs: Any) -> ToolSpec | None:
        nonlocal asked
        asked = True
        return _stub_code_tool()

    monkeypatch.setattr(app.AGENT_FUNCTION, "build_task_v3_code_tool", _build)
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", True)

    for surface in ("add", "replace"):
        monkeypatch.setattr(settings, "TASK_V3_CODE_TOOL_SURFACE", surface)
        caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])
        await _run_to_finish(caller)

        assert not asked, surface
        # And `replace` did not strip the action tools on its way to offering nothing.
        assert _advertised(caller) == _SURFACE_OFF_TOOL_NAMES | {"finish"}, surface


@pytest.mark.asyncio
async def test_frame_perception_per_run_pin_withholds_the_code_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guarantee as `test_frame_perception_withholds_the_code_tool`, but for the per-run arm.

    The env override is the force-on term; a run pinned on by the per-run resolver instead (env
    False) must be withheld identically, or the code tool ends up gated on how the run was turned
    on rather than on whether it was.
    """
    asked = False

    async def _build(**kwargs: Any) -> ToolSpec | None:
        nonlocal asked
        asked = True
        return _stub_code_tool()

    monkeypatch.setattr(app.AGENT_FUNCTION, "build_task_v3_code_tool", _build)
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)

    for surface in ("add", "replace"):
        monkeypatch.setattr(settings, "TASK_V3_CODE_TOOL_SURFACE", surface)
        caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])
        await _run_to_finish(caller, frame_perception=True)

        assert not asked, surface
        # And `replace` did not strip the action tools on its way to offering nothing.
        assert _advertised(caller) == _SURFACE_OFF_TOOL_NAMES | {"finish"}, surface


@pytest.mark.asyncio
async def test_no_runner_leaves_every_surface_with_todays_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's actual state: the deployment has no runner, so the hook returns None.

    The engine-level branch, not the helper's -- this is the path every run takes right now, and
    `replace` reaching it must still leave the model able to act.
    """

    async def _build(**kwargs: Any) -> ToolSpec | None:
        return None

    monkeypatch.setattr(app.AGENT_FUNCTION, "build_task_v3_code_tool", _build)
    for surface in ("add", "replace"):
        monkeypatch.setattr(settings, "TASK_V3_CODE_TOOL_SURFACE", surface)
        caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])
        await _run_to_finish(caller)

        assert _advertised(caller) == _SURFACE_OFF_TOOL_NAMES | {"finish"}, surface


@pytest.mark.asyncio
async def test_a_raising_code_tool_hook_costs_the_tool_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`add` is meant to be purely additive, so a sandbox hiccup must not fail an otherwise fine run.

    Asserted on the run's outcome as well as the advertised set: a withheld tool that still let the
    exception escape would fail the task, which is the failure mode worth naming.
    """

    async def _build(**kwargs: Any) -> ToolSpec | None:
        raise RuntimeError("sandbox provisioning blew up")

    monkeypatch.setattr(app.AGENT_FUNCTION, "build_task_v3_code_tool", _build)
    monkeypatch.setattr(settings, "TASK_V3_CODE_TOOL_SURFACE", "add")
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])

    skyvern_context.set(SkyvernContext(task_id="tsk_surface_raise"))
    try:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=caller,
            goal="Do the thing.",
            starting_url="https://example.test/",
        )
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert _advertised(caller) == _SURFACE_OFF_TOOL_NAMES | {"finish"}


@pytest.mark.asyncio
async def test_a_run_with_no_identity_is_not_given_a_code_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment keys a sandbox session on the run identity, so no identity means no tool.

    Two runs sharing an empty identity would share a session. Withholding is the only answer that
    cannot produce a collision.
    """
    asked = False

    async def _build(**kwargs: Any) -> ToolSpec | None:
        nonlocal asked
        asked = True
        return _stub_code_tool()

    monkeypatch.setattr(app.AGENT_FUNCTION, "build_task_v3_code_tool", _build)
    monkeypatch.setattr(settings, "TASK_V3_CODE_TOOL_SURFACE", "add")
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])

    skyvern_context.set(SkyvernContext())
    try:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=caller,
            goal="Do the thing.",
            starting_url="https://example.test/",
        )
    finally:
        skyvern_context.reset()

    assert not asked
    assert _advertised(caller) == _SURFACE_OFF_TOOL_NAMES | {"finish"}


# The combobox bullet, pinned verbatim. This bullet produced FIVE defects in one PR, every one of them
# a claim that some `select_combobox` error class falsifies -- only 4 of its 18 classes are row-related,
# so guidance quantifying over them is wrong for most. A phrase blacklist cannot fire on wording nobody
# anticipated ("every refusal provides a recovery action" would pass and is false), so the enforcing
# mechanism is a snapshot: it fires on ALL change, which is the point for this text and not a cost.
_COMBOBOX_BULLET = (
    "- Autocomplete / typeahead / combobox fields (location, school, employer lookups) render suggestions only "
    "AFTER you type, and the raw text you type is NOT accepted until you pick a suggestion. Use the "
    "`select_combobox` tool (selector + value) for these — it types, waits for the suggestions to render, selects "
    "the best-matching one, and verifies the field committed. Do NOT `type` into them or press keys on your own "
    "initiative. If `select_combobox` returns an error, the field is genuinely unfilled — never treat it as done. "
    "Act on what that error tells you rather than substituting a value of your own: this field commits only the "
    "suggestions the page itself offers, and those are often coarser than the value you hold."
)


def test_combobox_bullet_is_pinned_so_every_edit_is_re_derived_against_the_error_taxonomy() -> None:
    """The property, which the snapshot enforces rather than expresses: the bullet may claim what the
    MODEL should do, never what an ERROR CONTAINS, and any prohibition is scoped by PROVENANCE (a value
    or keystroke the model originates) rather than by shape. Four wordings broke the first rule -- "try
    a fuller value", "pass a listed row's text back" (identical_rows wants a click), "the error states a
    step" (row-less commit failures state none), "do not retype a longer value" (ambiguous_rows' own
    next_step IS longer) -- and the unconditional typing ban broke the second, contradicting
    identical_rows' "type the value to reopen the list" (tools.py:666).

    WHAT THIS IS: a change-detector, not a correctness test. It cannot tell a semantic defect from a
    rewording -- editing this constant alongside a broken prompt restores green. All it does is force a
    human to look, which is the most any test here can do.

    WHY EXACT PROSE, given CLAUDE.md:129 ("do not assert exact prompt prose ... WHEN A BEHAVIOR/CONTRACT
    ASSERTION EXISTS"): that precondition is not met, and the claim is checkable. Every browser e2e
    substitutes `ScriptedLLMCaller` for the model -- test_taskv3_fixture_parity.py says so in its own
    docstring -- so no test in this repo can assert prompt-driven behaviour. The structural fix (a
    mechanical guidance-vs-next_step consistency check) is tracked as SKY-16299, not built here.

    Updating this snapshot is not a formality: re-derive the new text against every error class in
    tools.py first, and against the WHOLE bullet -- a new clause can falsify an older one, which is how
    the pre-existing unconditional typing ban became a live contradiction."""
    bullet = next(line for line in SYSTEM_PROMPT.splitlines() if "select_combobox` tool" in line)

    assert bullet == _COMBOBOX_BULLET


@pytest.mark.asyncio
async def test_engine_restores_a_blank_working_page_before_the_block_hands_it_on(tmp_path: Path) -> None:
    # `finish` is assembled outside `build_browser_tools` (engine: browser_tools + extras + finish)
    # and is therefore never wrapped, so a block shaped `click(download) -> finish` reaches the end
    # of the loop with the tab still on `about:blank`. The next url-less block inherits it and
    # `resolve_inherited_workflow_task_page` raises InvalidWorkflowTaskURLState (SKY-16322). Only the
    # post-loop backstop can repair this shape -- the per-call guard has no later call to run on.
    page = _DownloadFakePage(tmp_path)
    page._click_blanks = True
    before = page.url
    restored: list[tuple[Any, str]] = []

    async def _restore(target: Any, url: str) -> None:
        restored.append((target, url))
        target.url = url

    script = [
        [("click", {"selector": "#dl"})],
        [("finish", {"status": "completed", "reason": "downloaded the statement"})],
    ]
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(page),
        llm_caller=_ScriptedCaller(script),
        goal="Download the statement.",
        restore_page_url=_restore,
    )

    assert outcome.status == "completed"
    assert restored == [(page, before)]
    assert page.url == before


@pytest.mark.asyncio
async def test_engine_repairs_a_blank_page_when_the_loop_ends_without_another_tool_call(
    tmp_path: Path,
) -> None:
    # The per-call guard repairs BEFORE a tool runs, so it cannot help when the blanking click is the
    # last thing that happens -- the turn budget runs out and the loop returns with the page still
    # blank. Only the post-loop backstop covers that, and the next url-less block is what pays.
    page = _DownloadFakePage(tmp_path)
    page._click_blanks = True
    before = page.url
    restored: list[tuple[Any, str]] = []

    async def _restore(target: Any, url: str) -> None:
        restored.append((target, url))
        target.url = url

    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(page),
        llm_caller=_ScriptedCaller([[("click", {"selector": "#dl"})]]),
        goal="Download the statement.",
        restore_page_url=_restore,
        max_turns=1,
    )

    assert outcome.status != "completed"  # ran out of turns rather than finishing
    assert restored == [(page, before)]
    assert page.url == before


def test_caller_level_bridge_check_denies_a_router_config_with_a_non_azure_api_base() -> None:
    # The single-config check reads api_base off the config; a router config carries it per
    # deployment instead, so a router pointed at OpenRouter used to slip past and report that it
    # bridges. It does not, and the dict reasoning_effort that verdict unlocks 400s there.
    from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
    from skyvern.schemas.llm import LLMRouterConfig, LLMRouterModelConfig

    def _router(
        model: str,
        api_base: str | None,
        fallback_model: str | None = None,
        fallback_api_base: str | None = None,
    ) -> LLMRouterConfig:
        return LLMRouterConfig(
            model_name="group-flex-fallback-router",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            model_list=[
                LLMRouterModelConfig(
                    model_name="group-flex",
                    litellm_params={"model": model, "api_base": api_base, "service_tier": "flex"},
                ),
                LLMRouterModelConfig(
                    model_name="group-fallback",
                    litellm_params={
                        "model": fallback_model if fallback_model is not None else model,
                        "api_base": fallback_api_base if fallback_model is not None else api_base,
                    },
                ),
            ],
            main_model_group="group-flex",
            fallback_model_group="group-fallback",
        )

    def _verdict(config: LLMRouterConfig) -> bool:
        return LLMCaller.uses_openai_responses_bridge(
            SimpleNamespace(openai_client=None, _custom_openrouter=False, llm_config=config, original_llm_key="K")
        )

    assert _verdict(_router("openrouter/openai/gpt-5.6-luna", "https://openrouter.ai/api/v1")) is False
    # the two shapes that must keep bridging: real OpenAI needs no api_base, and Azure serves it
    assert _verdict(_router("gpt-5.6-luna", None)) is True
    assert _verdict(_router("azure/gpt-5.6-luna", "https://example.openai.azure.com")) is True

    # A MIXED router: one deployment can serve the bridge and one cannot. This is what separates
    # `any` from `all` — the router picks the deployment, the caller cannot, so one leg that
    # would reject the dict form has to deny the whole config. Without this case, swapping the
    # guard to `all` leaves the test green.
    assert (
        _verdict(
            _router(
                "gpt-5.6-luna",
                None,
                fallback_model="openrouter/openai/gpt-5.6-luna",
                fallback_api_base="https://openrouter.ai/api/v1",
            )
        )
        is False
    )


def test_dispatchable_deployments_covers_every_fallback_group_shape() -> None:
    # The bridge guard judges a router by the deployments it can actually serve a call from.
    # No config in the repo today has a deployment outside its groups, or a list-valued fallback,
    # so these branches are only reachable from here — and a guard that silently widens or
    # narrows its own input would still look correct on every existing config.
    from skyvern.forge.sdk.api.llm.api_handler_factory import _dispatchable_deployments
    from skyvern.schemas.llm import LLMRouterConfig, LLMRouterModelConfig

    def _leg(name: str) -> LLMRouterModelConfig:
        return LLMRouterModelConfig(model_name=name, litellm_params={"model": "m"})

    def _config(fallback: str | list[str] | None, names: list[str]) -> LLMRouterConfig:
        return LLMRouterConfig(
            model_name="router",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            model_list=[_leg(name) for name in names],
            main_model_group="main",
            fallback_model_group=fallback,
        )

    def _names(fallback: str | list[str] | None, names: list[str]) -> set[str]:
        return {d.model_name for d in _dispatchable_deployments(_config(fallback, names))}

    # a deployment in neither group is unreachable and must be excluded
    assert _names("fb", ["main", "fb", "retired"]) == {"main", "fb"}
    # every fallback shape the type allows
    assert _names(None, ["main", "fb"]) == {"main"}
    assert _names([], ["main", "fb"]) == {"main"}
    assert _names(["fb1", "fb2"], ["main", "fb1", "fb2", "other"]) == {"main", "fb1", "fb2"}
    # the main group is never dropped
    assert "main" in _names(["fb1"], ["main", "fb1"])


STUB_REQUIRED_FIELD_ANSWERS_FILL = "prefer the provided values. Stub fill rule. If one of those is required, "
STUB_SELF_SCREEN_BULLET = "- Stub self-screen bullet.\n"


@pytest.fixture
def stub_required_field_answers_text(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Stands in for a deployment that supplies the required-field-answers text; OSS supplies none."""
    texts = (STUB_REQUIRED_FIELD_ANSWERS_FILL, STUB_SELF_SCREEN_BULLET)
    monkeypatch.setattr(app.AGENT_FUNCTION, "task_v3_required_field_answers_text", lambda: texts)
    return texts


async def _system_prompt_for_run(*, arm: str | None, required_field_answers_arm: str | None = None) -> str:
    """The system message an actual engine run sends, with the remedy and required-field-answers arms pinned."""
    context = SkyvernContext()
    if arm is not None:
        context.run_arms = {**context.run_arms, UNANSWERABLE_FIELD_REMEDY_FLAG: ("wr_1", arm)}
    if required_field_answers_arm is not None:
        context.run_arms = {**context.run_arms, REQUIRED_FIELD_ANSWERS_FLAG: ("wr_1", required_field_answers_arm)}
    skyvern_context.set(context)
    try:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
            goal="noop",
        )
    finally:
        skyvern_context.reset()
    return next(m for m in outcome.messages if m.get("role") == "system")["content"]


_DATE_MARKER = "\n\nToday's date is "


@pytest.mark.asyncio
@pytest.mark.parametrize("required_field_answers_arm", [None, "control", "unrandomized"])
@pytest.mark.parametrize("arm", [None, "control", "unrandomized"])
async def test_unanswerable_field_remedy_off_arms_send_todays_prompt_unchanged(
    arm: str | None, required_field_answers_arm: str | None, stub_required_field_answers_text: tuple[str, str]
) -> None:
    # The off arms are the deployed prompt, byte for byte: a run outside the experiment must not be
    # able to drift because the experiment exists.
    system_prompt = await _system_prompt_for_run(arm=arm, required_field_answers_arm=required_field_answers_arm)
    assert system_prompt.startswith(SYSTEM_PROMPT)
    assert UNANSWERABLE_FIELD_REMEDY_CONTROL in system_prompt
    assert UNANSWERABLE_FIELD_REMEDY_TREATMENT not in system_prompt
    assert system_prompt_for_run_arms(required_field_answers_text=None, unanswerable_field_remedy=False) is (
        SYSTEM_PROMPT
    )


@pytest.mark.asyncio
async def test_unanswerable_field_remedy_treatment_swaps_the_remedy_and_nothing_else() -> None:
    control = await _system_prompt_for_run(arm="control")
    treatment = await _system_prompt_for_run(arm="treatment")

    assert control.count(UNANSWERABLE_FIELD_REMEDY_CONTROL) == 1
    assert UNANSWERABLE_FIELD_REMEDY_CONTROL not in treatment
    assert UNANSWERABLE_FIELD_REMEDY_TREATMENT in treatment
    # The ONLY difference between the arms is the remedy clause. Anything else the arm changed --
    # including the do-not-invent rule the clause hangs off -- reds here. The date suffix the engine
    # appends is dropped: the two prompts are built by separate calls, so a midnight crossing
    # between them would otherwise red this on wall-clock rather than on a real difference.
    control_body = control.split(_DATE_MARKER)[0]
    treatment_body = treatment.split(_DATE_MARKER)[0]
    assert _DATE_MARKER in control and _DATE_MARKER in treatment
    assert control_body != treatment_body
    assert control_body.replace(UNANSWERABLE_FIELD_REMEDY_CONTROL, UNANSWERABLE_FIELD_REMEDY_TREATMENT) == (
        treatment_body
    )


@pytest.mark.asyncio
async def test_required_field_answers_treatment_suppresses_the_remedy_swap(
    stub_required_field_answers_text: tuple[str, str],
) -> None:
    # The remedy's leave-blank clause collided with page validation and the model refilled a legal-status
    # answer; under required-field-answers its own stop clause must hold, so C4 sends C3's prompt.
    with capture_logs() as logs:
        both = await _system_prompt_for_run(arm="treatment", required_field_answers_arm="treatment")
    required_only = await _system_prompt_for_run(arm="control", required_field_answers_arm="treatment")

    assert both.split(_DATE_MARKER)[0] == required_only.split(_DATE_MARKER)[0]
    assert STUB_SELF_SCREEN_BULLET in both
    assert both.count(UNANSWERABLE_FIELD_REMEDY_CONTROL) == 1
    assert UNANSWERABLE_FIELD_REMEDY_TREATMENT not in both
    assert [e["event"] for e in logs].count(
        "Task V3 unanswerable-field remedy suppressed by required-field-answers arm"
    ) == 1


@pytest.mark.asyncio
async def test_required_field_answers_treatment_is_not_the_silent_fallback(
    stub_required_field_answers_text: tuple[str, str],
) -> None:
    # The builder falls back to today's prompt when an anchor stops matching; a green suite must not
    # hide a treatment arm that is byte-identical to control.
    treatment = await _system_prompt_for_run(arm=None, required_field_answers_arm="treatment")
    control = await _system_prompt_for_run(arm=None, required_field_answers_arm="control")
    treatment_body = treatment.split(_DATE_MARKER)[0]

    assert not treatment.startswith(SYSTEM_PROMPT)
    assert STUB_REQUIRED_FIELD_ANSWERS_FILL in treatment_body and STUB_SELF_SCREEN_BULLET in treatment_body
    assert treatment_body.count(UNANSWERABLE_FIELD_REMEDY_CONTROL) == 1
    assert (
        treatment_body.replace(STUB_REQUIRED_FIELD_ANSWERS_FILL, REQUIRED_FIELD_ANSWERS_ANCHOR).replace(
            STUB_SELF_SCREEN_BULLET, ""
        )
        == control.split(_DATE_MARKER)[0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remedy_arm", "expected_prompt"),
    [("control", SYSTEM_PROMPT), ("treatment", UNANSWERABLE_FIELD_REMEDY_PROMPT)],
    ids=["remedy_control", "remedy_treatment"],
)
async def test_required_field_answers_treatment_without_supplied_text_renders_control(
    remedy_arm: str, expected_prompt: str
) -> None:
    # OSS supplies no required-field-answers text, so that arm's treatment must be indistinguishable from
    # its control -- including leaving the remedy arm free to apply -- and say so once for the read to drop.
    with capture_logs() as logs:
        system_prompt = await _system_prompt_for_run(arm=remedy_arm, required_field_answers_arm="treatment")

    assert system_prompt.split(_DATE_MARKER)[0] == expected_prompt
    events = [e["event"] for e in logs]
    assert (
        events.count("Task V3 required-field-answers arm resolved treatment but no text is supplied; sent control") == 1
    )
    assert "Task V3 unanswerable-field remedy suppressed by required-field-answers arm" not in events


@pytest.mark.parametrize(
    "drifted_prompt",
    [
        SYSTEM_PROMPT.replace(REQUIRED_FIELD_ANSWERS_ANCHOR, ""),
        SYSTEM_PROMPT.replace(SELF_SCREEN_ANCHOR, ""),
        SYSTEM_PROMPT + REQUIRED_FIELD_ANSWERS_ANCHOR,
    ],
)
def test_required_field_answers_falls_back_to_control_when_an_anchor_drifts(
    monkeypatch: pytest.MonkeyPatch, drifted_prompt: str
) -> None:
    engine_mod._build_required_field_answers_prompt.cache_clear()
    monkeypatch.setattr(engine_mod, "SYSTEM_PROMPT", drifted_prompt)
    try:
        with capture_logs() as logs:
            prompt = system_prompt_for_run_arms(
                required_field_answers_text=(STUB_REQUIRED_FIELD_ANSWERS_FILL, STUB_SELF_SCREEN_BULLET),
                unanswerable_field_remedy=False,
            )
    finally:
        engine_mod._build_required_field_answers_prompt.cache_clear()
    assert prompt is drifted_prompt
    assert [e["event"] for e in logs] == ["Task V3 required-field-answers clause is not uniquely present; sent control"]


def test_unanswerable_field_remedy_clause_stays_uniquely_present_and_the_rule_is_not_gated() -> None:
    # If the prompt is edited so the clause no longer matches, the treatment arm silently becomes a
    # no-op and the experiment reads null for the wrong reason. This reds on that edit.
    assert SYSTEM_PROMPT.count(UNANSWERABLE_FIELD_REMEDY_CONTROL) == 1
    # The rule the remedy hangs off is the safety property and is NOT part of the variable.
    rule = "Do not invent sensitive or identifying values (government IDs, financial details, or legal/eligibility attestations)"
    assert rule in SYSTEM_PROMPT
    assert rule in system_prompt_for_run_arms(required_field_answers_text=None, unanswerable_field_remedy=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("required_field_answers_arm", [None, "treatment"])
async def test_unanswerable_field_remedy_treatment_adds_no_submit_pressure(
    required_field_answers_arm: str | None,
    stub_required_field_answers_text: tuple[str, str],
) -> None:
    # The charter's non-negotiable: while the only thing standing between a model error and an
    # unauthorized submit is a line of system prompt, no arm may add prose that competes with it.
    # An earlier revision ended the treatment with "report the task complete only if the page itself
    # accepted the submission", which on a goal that never asked for a submission reads as the only
    # route to success running through one. Anti-relabelling never needed it: the ungated
    # how-to-work bullet below already requires every required field to hold its value before
    # completed, in BOTH arms. Asserted on the prompt the engine actually sends, not the constant.
    # The supplied required-field-answers wording itself is pinned against this where it lives.
    treatment = await _system_prompt_for_run(arm="treatment", required_field_answers_arm=required_field_answers_arm)
    control = await _system_prompt_for_run(arm="control", required_field_answers_arm=required_field_answers_arm)
    base_control = await _system_prompt_for_run(arm="control")
    bullet = next(line for line in treatment.splitlines() if line.startswith("- Fill fields from the task's data"))

    assert "submission" not in bullet and "accepted" not in bullet
    # The optional-fields instruction shares the bullet and must stay untouched by the remedy.
    assert "Leave optional fields blank" in bullet
    # The anti-relabelling property the removed clause used to carry, in its real home: ungated, and
    # byte-identical across the arms, so neither arm carries a completion rule the other does not.
    contract = next(line for line in treatment.splitlines() if "status=completed" in line)
    assert "every required field holds its intended value" in contract
    assert contract in control and contract in base_control
    # The no-submit rule is the guard the charter is protecting; it must survive the swap intact.
    no_submit = "Do not submit forms or take irreversible actions unless the goal explicitly instructs it."
    assert no_submit in treatment and no_submit in control and no_submit in base_control


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_type", "has_navigation_goal", "expected_hold"),
    [
        # SKY-16651's measured specimen is navigation blocks; "task block" in its analysis meant any
        # block that runs a task, not BlockType.TASK alone.
        ("navigation", True, True),
        ("task", True, True),
        # A TASK BLOCK WITH NO navigation_goal is read-only by construction -- TaskBlockYAML allows
        # a data_extraction_goal alone, and agent.py keys its own `is_extraction_task` on exactly
        # this field -- so it is not the specimen SKY-16651 measured. NOT an authorization test:
        # nothing in the block schema establishes authorization to mutate a page, which is why the
        # held message directs no action rather than being gated on a signal that cannot bear it.
        ("task", False, False),
        # A BARE TASK carries block_type=None. An exclusion list let it through, which is why the
        # predicate is an allowlist and anything unenumerated fails closed.
        (None, True, False),
        # An extraction block is refused the fill tools, so "observed and attempted nothing" is its
        # correct shape, and it is outside the measured population.
        ("extraction", True, False),
        ("validation", True, False),
        ("login", True, False),
    ],
)
async def test_no_action_hold_is_offered_only_to_the_measured_block_population(
    monkeypatch: pytest.MonkeyPatch, block_type: str | None, has_navigation_goal: bool, expected_hold: bool
) -> None:
    monkeypatch.setattr(settings, "TASK_V3_NO_ACTION_HOLD", True)
    finish_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        return LoopOutcome(status="completed", reason="ok")

    real_make_finish_tool = engine_mod.make_finish_tool

    def capturing_make_finish_tool(*args: Any, **kwargs: Any) -> Any:
        finish_kwargs.update(kwargs)
        return real_make_finish_tool(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    monkeypatch.setattr(engine_mod, "make_finish_tool", capturing_make_finish_tool)

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="read what the page says",
        block_type=block_type,
        has_navigation_goal=has_navigation_goal,
    )

    assert finish_kwargs["no_action_hold"] is expected_hold


def _provider_503() -> Exception:
    return litellm.exceptions.InternalServerError(message="upstream 503", llm_provider="openai", model="gpt-4")


def _finish_completion() -> litellm.ModelResponse:
    finish_call = {
        "id": "call_0",
        "type": "function",
        "function": {"name": "finish", "arguments": json.dumps({"status": "completed", "reason": "done"})},
    }
    return litellm.ModelResponse(
        model="gpt-4",
        choices=[
            {"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [finish_call]}}
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


async def _run_v3_against_provider(monkeypatch: pytest.MonkeyPatch, provider: AsyncMock) -> tuple[LoopOutcome, list]:
    # A real LLMCaller with only the provider round-trip faked, so the engine's wiring, the loop's
    # retry, and the handler seam all decide together whether a receipt is emitted.
    llm_config = LLMConfig(model_name="gpt-4", required_env_vars=[], supports_vision=False, add_assistant_prefix=False)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    caller = LLMCaller("TEST_TASKV3_EXHAUSTION")
    monkeypatch.setattr(caller, "_dispatch_llm_call", provider)
    monkeypatch.setattr(loop_mod, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    with capture_logs() as logs:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="noop"
        )
    receipts = [log for log in logs if log["event"] == LLM_RETRY_CHAIN_EXHAUSTED_MESSAGE]
    return outcome, receipts


@pytest.mark.asyncio
async def test_llm_failure_the_loop_retry_recovers_is_not_counted_as_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AsyncMock(side_effect=[_provider_503(), _provider_503(), _finish_completion()])

    outcome, receipts = await _run_v3_against_provider(monkeypatch, provider)

    assert outcome.status == "completed"
    assert receipts == []


@pytest.mark.asyncio
async def test_llm_failure_past_every_retry_is_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AsyncMock(side_effect=[_provider_503() for _ in range(10)])

    outcome, receipts = await _run_v3_against_provider(monkeypatch, provider)

    assert outcome.status == "loop_error"
    assert [(receipt["llm_key"], receipt["outcome"]) for receipt in receipts] == [
        ("TEST_TASKV3_EXHAUSTION", "provider_error")
    ]


def _gemini_then_gpt_chain() -> LLMRouterConfig:
    return LLMRouterConfig(
        model_name="test-router",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(model_name="gemini-group", litellm_params={"model": "vertex_ai/gemini-x"}),
            LLMRouterModelConfig(model_name="gpt-group", litellm_params={"model": "azure/gpt-x"}),
        ],
        main_model_group="gemini-group",
        fallback_model_group="gpt-group",
        routing_strategy="simple-shuffle",
        num_retries=0,
        disable_cooldowns=True,
        temperature=None,
    )


# Each provider call, in order, answers (True) or 503s (False). The first two calls are one loop
# attempt that exhausts the router's chain; the loop then re-issues the same request.
V3_FALLBACK_CASES = [
    pytest.param(
        [False, False, False, True],
        "completed",
        [("recovered", "gemini-group", "gpt-group")],
        id="retry_recovered_by_the_backup_model_counts_once",
    ),
    pytest.param(
        [False, False, True],
        "completed",
        [("primary", "gemini-group", "gemini-group")],
        id="retry_on_the_same_model_is_not_a_fallback",
    ),
    pytest.param([False] * 6, "loop_error", [("exhausted", "gemini-group", None)], id="exhausted_is_not_a_recovery"),
]


@pytest.mark.parametrize(("answers", "status", "expected"), V3_FALLBACK_CASES)
@pytest.mark.asyncio
async def test_llm_fallback_outcome_is_reported_once_per_call_intent(
    monkeypatch: pytest.MonkeyPatch, answers: list[bool], status: str, expected: list[tuple[str, str, str | None]]
) -> None:
    # A real LLMCaller over a real litellm.Router, faking only each provider answer, so the router's
    # fallback, the loop's retry, and both receipts decide together what the metric counts.
    real_acompletion = litellm.acompletion
    script = list(answers)

    async def provider(**kwargs: Any) -> Any:
        answer = _finish_completion() if script.pop(0) else "litellm.InternalServerError"
        return await real_acompletion(**kwargs, mock_response=answer)

    monkeypatch.setattr(litellm, "acompletion", provider)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: _gemini_then_gpt_chain())
    monkeypatch.setattr(api_handler_factory, "_LLMCALLER_ROUTER_CACHE", {})
    monkeypatch.setattr(loop_mod, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    with capture_logs() as logs:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()), llm_caller=LLMCaller("TEST_TASKV3_FALLBACK"), goal="noop"
        )

    assert outcome.status == status
    assert fallback_receipts(logs) == expected


async def _no_download_pending(_staged: frozenset[str]) -> str | None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "judged"),
    [
        ({}, 1),
        ({"completion_blocker": _no_download_pending}, 0),
        ({"extraction_requested": True}, 0),
        ({"page_free": True}, 0),
    ],
    ids=["navigation", "download_gated", "extraction", "page_free"],
)
async def test_goal_check_skips_blocks_that_verify_their_own_completion(scope: dict[str, Any], judged: int) -> None:
    prompts: list[str] = []

    async def judge(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"verdict": "achieved", "quote": "", "missing": ""}

    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([[("observe", {})], [("finish", {"status": "completed", "reason": "done"})]]),
        goal="Open the settings page.",
        goal_judge=judge,
        goal_check_enforce=True,
        **scope,
    )

    assert outcome.status == "completed"
    assert len(prompts) == judged
    assert (outcome.goal_check is not None) == bool(judged)


@pytest.mark.asyncio
@pytest.mark.parametrize(("deadline_seconds", "judged"), [(10.0, True), (1.0, False)])
async def test_goal_check_timeout_is_bounded_by_the_runs_deadline(
    monkeypatch: pytest.MonkeyPatch, deadline_seconds: float, judged: bool
) -> None:
    timeouts: list[float] = []
    real_run_goal_check = engine_mod.run_goal_check

    async def recording_run_goal_check(**kwargs: Any) -> Any:
        timeouts.append(kwargs["timeout_seconds"])
        return await real_run_goal_check(**kwargs)

    monkeypatch.setattr(engine_mod, "run_goal_check", recording_run_goal_check)
    prompts: list[str] = []

    async def judge(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"verdict": "achieved", "quote": "", "missing": ""}

    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
        goal="Open the settings page.",
        goal_judge=judge,
        goal_check_enforce=True,
        deadline_seconds=deadline_seconds,
    )

    assert outcome.status == "completed"
    assert outcome.goal_check is not None
    if judged:
        (timeout,) = timeouts
        assert 0 < timeout <= deadline_seconds - engine_mod.GOAL_CHECK_DEADLINE_MARGIN_SECONDS
        assert len(prompts) == 1
    else:
        # One second left is inside the margin: the judge is never called, and the check says why.
        assert timeouts == []
        assert prompts == []
        assert outcome.goal_check["last_skipped_reason"] == "deadline"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enforce", "second", "holds", "would_holds", "would_fails"),
    [
        (True, "achieved", 1, 0, 0),
        (False, "achieved", 0, 1, 0),
        (False, "not_achieved", 0, 1, 1),
    ],
)
async def test_goal_check_summary_separates_real_holds_from_shadow_ones(
    enforce: bool, second: str, holds: int, would_holds: int, would_fails: int
) -> None:
    verdicts = iter(["not_achieved", second])
    prompts: list[str] = []

    async def judge(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"verdict": next(verdicts), "quote": "SCREENSHOT: an empty form", "missing": "nothing saved"}

    finish = ("finish", {"status": "completed", "reason": "done"})
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([[finish], [finish]]),
        goal="Save the form.",
        goal_judge=judge,
        goal_check_enforce=enforce,
        goal_instructions="If the form is already saved, finish completed.",
    )

    assert outcome.status == "completed"
    assert outcome.goal_check is not None
    assert outcome.goal_check["holds"] == holds
    assert outcome.goal_check["would_holds"] == would_holds
    assert outcome.goal_check["would_fails"] == would_fails
    assert len(prompts) == 2
    # The summary describes the finish gate's decisions; the shadow re-check is counted apart.
    assert outcome.goal_check["checks"] == (2 if enforce else 1)
    assert outcome.goal_check["judged"] == (2 if enforce else 1)
    assert outcome.goal_check["rechecks"] == (0 if enforce else 1)
    assert outcome.goal_check["last_verdict"] == ("achieved" if enforce else "not_achieved")
    assert outcome.goal_check["last_action"] == ("accept" if enforce else "hold")
    assert "If the form is already saved, finish completed." in prompts[0]


@pytest.mark.asyncio
async def test_goal_check_redactor_reaches_the_judge_prompt() -> None:
    prompts: list[str] = []

    async def judge(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"verdict": "achieved", "quote": "", "missing": ""}

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
        goal="Enter the code Qz7Wk2Pm9Rt4.",
        goal_judge=judge,
        goal_check_redactor=lambda: lambda text: text.replace("Qz7Wk2Pm9Rt4", "[REDACTED_SECRET]"),
    )

    (prompt,) = prompts
    assert "Enter the code [REDACTED_SECRET]." in prompt
    assert "Qz7Wk2Pm9Rt4" not in prompt


def _code_delivering_tool() -> ToolSpec:
    async def handler(args: dict[str, Any]) -> ToolResult:
        ctx = skyvern_context.current()
        assert ctx is not None
        ctx.register_secret_value("482913")
        return ToolResult.ok("verification_code: 482913")

    return ToolSpec(
        name="fetch_code", description="fetch_code", parameters={"type": "object", "properties": {}}, handler=handler
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "on_page_at_start", "judged"),
    [
        (("type", {"selector": "#first", "text": "John"}), False, True),
        (("type", {"selector": "#first", "text": f"{RANDOM_SECRET_ID_PREFIX}password_1"}), False, False),
        (("fetch_code", {}), False, False),
        (("navigate", {"url": f"https://example.test/login?t={RANDOM_SECRET_ID_PREFIX}token_1"}), False, False),
        (("type", {"selector": "#first", "text": "John"}), True, False),
    ],
    ids=["plain_type", "credential_placeholder", "verification_code", "navigate_placeholder", "already_on_page"],
)
async def test_goal_check_never_judges_a_run_that_entered_a_secret(
    step: tuple[str, Any], on_page_at_start: bool, judged: bool
) -> None:
    # The finish screenshot could show the secret, and v3 tools apply no visual secret mask.
    prompts: list[str] = []

    async def judge(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"verdict": "achieved", "quote": "", "missing": ""}

    skyvern_context.set(SkyvernContext())
    try:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            # An observe after the secret: the flag must outlive the entry that set it.
            llm_caller=_ScriptedCaller(
                [[step], [("observe", {})], [("finish", {"status": "completed", "reason": "done"})]]
            ),
            goal="Sign in.",
            extra_tools=[_code_delivering_tool()],
            goal_judge=judge,
            goal_check_enforce=True,
            secret_on_page_at_start=on_page_at_start,
        )
    finally:
        skyvern_context.reset()

    assert outcome.status == "completed"
    assert outcome.goal_check is not None
    assert len(prompts) == (1 if judged else 0)
    assert outcome.goal_check["last_skipped_reason"] == (None if judged else "secret_entered")


@pytest.mark.asyncio
@pytest.mark.parametrize(("extra_chars", "judged"), [(0, True), (1, False)], ids=["at_cap", "over_cap"])
async def test_goal_check_never_judges_against_truncated_instructions(extra_chars: int, judged: bool) -> None:
    # A rule that decides "done" can sit past the cap; judging against a prefix could fail a correct completion.
    prompts: list[str] = []

    async def judge(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"verdict": "achieved", "quote": "", "missing": ""}

    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
        goal="Save the form.",
        goal_judge=judge,
        goal_check_enforce=True,
        goal_instructions="r" * (INSTRUCTIONS_MAX_CHARS + extra_chars),
    )

    assert outcome.status == "completed"
    assert outcome.goal_check is not None
    assert len(prompts) == (1 if judged else 0)
    assert outcome.goal_check["last_skipped_reason"] == (None if judged else "instructions_too_long")


@pytest.mark.asyncio
async def test_goal_check_measures_instructions_after_redaction() -> None:
    # Redaction can lengthen text (a short secret becomes a longer marker), and the judge sees the redacted text.
    prompts: list[str] = []

    async def judge(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"verdict": "achieved", "quote": "", "missing": ""}

    def redactor() -> Callable[[str], str]:
        return lambda text: text.replace("PIN", "[REDACTED_SECRET]")

    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
        goal="Save the form.",
        goal_judge=judge,
        goal_check_enforce=True,
        goal_instructions="r" * (INSTRUCTIONS_MAX_CHARS - 3) + "PIN",
        goal_check_redactor=redactor,
    )

    assert outcome.goal_check is not None
    assert prompts == []
    assert outcome.goal_check["last_skipped_reason"] == "instructions_too_long"
