"""Integration tests for validation evidence router wiring inside
``ForgeAgent._build_extract_action_prompt``.

These tests exercise the *agent-level* contract that the router decision flows
back to the caller as a ``PromptBuildResult.without_page_information`` field, so the
LLM call site can drop screenshots and the prompt builder can drop the
element tree. The router itself is mocked here; the router's own behavior is
covered by ``test_validation_evidence_router.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.constants import DEFAULT_MAX_TOKENS
from skyvern.forge import agent as agent_module
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.validation_evidence_router import (
    ValidationEvidenceKind,
    ValidationRouterDecision,
    ValidationRouterMode,
    ValidationRouterResult,
)
from skyvern.schemas.llm import LLMConfig, LLMRouterConfig, LLMRouterModelConfig
from tests.unit.helpers import make_organization, make_step, make_task


def _make_scraped_page() -> MagicMock:
    sp = MagicMock()
    sp.build_element_tree.return_value = "<div data-skyvern='1'>mock</div>"
    sp.build_lean_elements_tree.return_value = "<div>mock</div>"
    sp.screenshots = [b"img"]
    sp.elements = []
    sp.html = "<html><body><div>mock</div></body></html>"
    sp.last_used_element_tree_html = None
    return sp


def _make_browser_state() -> MagicMock:
    bs = MagicMock()
    page = AsyncMock()
    bs.get_working_page = AsyncMock(return_value=page)
    return bs


def _test_llm_config(model_family: str) -> LLMConfig | LLMRouterConfig:
    if model_family == "anthropic":
        return LLMRouterConfig(
            model_name="test-anthropic-router",
            required_env_vars=[],
            supports_vision=False,
            add_assistant_prefix=False,
            model_list=[
                LLMRouterModelConfig(
                    model_name="anthropic",
                    litellm_params={"model": "anthropic/claude-sonnet-test"},
                ),
                LLMRouterModelConfig(
                    model_name="bedrock",
                    litellm_params={"model": "bedrock/us.anthropic.claude-test"},
                ),
            ],
            main_model_group="anthropic",
            fallback_model_group="bedrock",
        )
    return LLMConfig(
        model_name={"gpt": "openai/gpt-5.5", "gemini": "gemini/gemini-test"}[model_family],
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
    )


@pytest.fixture
def patched_agent(monkeypatch: pytest.MonkeyPatch) -> ForgeAgent:
    agent = ForgeAgent()
    monkeypatch.setattr(agent, "_get_action_results", AsyncMock(return_value=""))
    monkeypatch.setattr(
        agent,
        "_build_navigation_payload",
        MagicMock(return_value={"extracted_amount": 100, "invoice_amount": 100}),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.SkyvernFrame.evaluate",
        AsyncMock(return_value="https://example.com/path"),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.build_open_tabs_context",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        agent,
        "_get_prompt_caching_settings",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.get_slim_output_template_value",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "get_extra_extract_action_guidance",
        AsyncMock(return_value=""),
    )
    return agent


def _stub_router_result(
    *,
    effective: bool,
    decision: ValidationRouterDecision,
    evidence_kind: ValidationEvidenceKind | None = None,
    confidence: float | None = None,
    mode: ValidationRouterMode = ValidationRouterMode.ENFORCE,
) -> ValidationRouterResult:
    return ValidationRouterResult(
        effective_without_page_information=effective,
        decision=decision,
        evidence_kind=evidence_kind,
        confidence=confidence,
        failure_reason=None,
        rationale="stub",
        mode=mode,
    )


@pytest.mark.parametrize(
    ("model_family", "uses_anthropic_cache"),
    [("anthropic", True), ("gpt", False), ("gemini", False)],
)
@pytest.mark.asyncio
async def test_extract_action_prompt_uses_resolved_anthropic_handler(
    patched_agent: ForgeAgent,
    monkeypatch: pytest.MonkeyPatch,
    model_family: str,
    uses_anthropic_cache: bool,
) -> None:
    llm_key = f"TEST_PROMPT_CACHE_{model_family.upper()}"
    monkeypatch.setitem(LLMConfigRegistry._configs, llm_key, _test_llm_config(model_family))
    monkeypatch.setattr(
        agent_module.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        MagicMock(return_value=SimpleNamespace(llm_key=llm_key)),
    )
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_type=TaskType.general)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)
    context = SkyvernContext(tz_info=None)

    with skyvern_context.scoped(context):
        result = await patched_agent._build_extract_action_prompt(
            task,
            step,
            _make_browser_state(),
            _make_scraped_page(),
        )

    assert (context.cached_static_prompt is not None) is uses_anthropic_cache
    assert result.use_caching is uses_anthropic_cache


@pytest.mark.asyncio
async def test_anthropic_split_prompt_above_economy_threshold_uses_full_render(
    patched_agent: ForgeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm_key = "TEST_PROMPT_CACHE_ECONOMY_ANTHROPIC"
    monkeypatch.setitem(LLMConfigRegistry._configs, llm_key, _test_llm_config("anthropic"))
    monkeypatch.setattr(
        agent_module.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        MagicMock(return_value=SimpleNamespace(llm_key=llm_key)),
    )
    monkeypatch.setattr(agent_module, "count_tokens", lambda _: DEFAULT_MAX_TOKENS + 1)

    class _FullPrompt(str):
        pass

    full_prompt = _FullPrompt()
    monkeypatch.setattr(agent_module, "load_prompt_with_elements", lambda **_: full_prompt)
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_type=TaskType.general)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    with skyvern_context.scoped(SkyvernContext(tz_info=None)):
        result = await patched_agent._build_extract_action_prompt(
            task,
            step,
            _make_browser_state(),
            _make_scraped_page(),
        )

    assert result.prompt is full_prompt


@pytest.mark.asyncio
async def test_validation_routed_data_only_drops_page_evidence(
    patched_agent: ForgeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """High-confidence data-only routing must:
    - flag ``PromptBuildResult.without_page_information=True``
    - render a prompt without the DOM element block
    - render a prompt without the page URL
    """
    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(
        now,
        org,
        task_type=TaskType.validation,
        complete_criterion="extracted_amount equals invoice_amount",
        terminate_criterion=None,
        navigation_goal=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    async def fake_router(**_: Any) -> ValidationRouterResult:
        return _stub_router_result(
            effective=True,
            decision=ValidationRouterDecision.DATA_ONLY_NO_PAGE,
            evidence_kind=ValidationEvidenceKind.DATA_ONLY,
            confidence=0.95,
        )

    monkeypatch.setattr("skyvern.forge.agent.resolve_validation_evidence_route", fake_router)

    ctx = SkyvernContext(tz_info=None)
    token = skyvern_context._context.set(ctx)
    try:
        build_result = await patched_agent._build_extract_action_prompt(
            task,
            step,
            _make_browser_state(),
            _make_scraped_page(),
        )
        prompt = build_result.prompt
        prompt_name = build_result.prompt_name
        without_page_information = build_result.without_page_information
    finally:
        skyvern_context._context.reset(token)

    assert without_page_information is True
    assert prompt_name == "decisive-criterion-validate"
    assert "data-skyvern" not in prompt, "element tree must not appear in no-page prompt"
    assert "https://example.com/path" not in prompt, "current_url must not appear in no-page prompt"


@pytest.mark.asyncio
async def test_validation_routed_page_aware_keeps_page_evidence(
    patched_agent: ForgeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAGE_AWARE decision (mixed / page_state / low conf / fallback) must
    keep the existing page-aware path: tuple flag False, prompt contains DOM
    and URL exactly as on main."""
    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(
        now,
        org,
        task_type=TaskType.validation,
        complete_criterion="The page shows a success message.",
        navigation_goal=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    async def fake_router(**_: Any) -> ValidationRouterResult:
        return _stub_router_result(
            effective=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            evidence_kind=ValidationEvidenceKind.PAGE_STATE,
            confidence=0.97,
        )

    monkeypatch.setattr("skyvern.forge.agent.resolve_validation_evidence_route", fake_router)

    ctx = SkyvernContext(tz_info=None)
    token = skyvern_context._context.set(ctx)
    try:
        build_result = await patched_agent._build_extract_action_prompt(
            task,
            step,
            _make_browser_state(),
            _make_scraped_page(),
        )
        prompt = build_result.prompt
        prompt_name = build_result.prompt_name
        without_page_information = build_result.without_page_information
    finally:
        skyvern_context._context.reset(token)

    assert without_page_information is False
    assert prompt_name == "decisive-criterion-validate"
    assert "data-skyvern" in prompt or "<div" in prompt, "element tree must be present in page-aware prompt"


@pytest.mark.asyncio
async def test_block_opt_in_drops_page_evidence_even_when_router_page_aware(
    patched_agent: ForgeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block-level opt-in (``ValidationBlock.without_page_information``,
    surfaced on the context as ``validation_without_page_information``) must
    drop page evidence even when the router stays page-aware (e.g. mode OFF).
    Proves the flag is OR'd into the prompt builder's decision (SKY-10593)."""
    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(
        now,
        org,
        task_type=TaskType.validation,
        complete_criterion="billing_date is within range and account_number matches",
        terminate_criterion=None,
        navigation_goal=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    async def fake_router(**_: Any) -> ValidationRouterResult:
        return _stub_router_result(
            effective=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            mode=ValidationRouterMode.OFF,
        )

    monkeypatch.setattr("skyvern.forge.agent.resolve_validation_evidence_route", fake_router)

    ctx = SkyvernContext(tz_info=None)
    ctx.validation_without_page_information = True
    token = skyvern_context._context.set(ctx)
    try:
        build_result = await patched_agent._build_extract_action_prompt(
            task,
            step,
            _make_browser_state(),
            _make_scraped_page(),
        )
    finally:
        skyvern_context._context.reset(token)

    assert build_result.without_page_information is True
    assert "data-skyvern" not in build_result.prompt, "element tree must not appear when block opts out of page info"
    assert "https://example.com/path" not in build_result.prompt, "current_url must not appear when block opts out"


@pytest.mark.asyncio
async def test_block_opt_in_default_off_keeps_page_evidence(
    patched_agent: ForgeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (flag unset) keeps today's page-aware behavior: page evidence is
    present and ``without_page_information`` stays False. Blast-radius guard."""
    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(
        now,
        org,
        task_type=TaskType.validation,
        complete_criterion="billing_date is within range and account_number matches",
        terminate_criterion=None,
        navigation_goal=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    async def fake_router(**_: Any) -> ValidationRouterResult:
        return _stub_router_result(
            effective=False,
            decision=ValidationRouterDecision.PAGE_AWARE,
            mode=ValidationRouterMode.OFF,
        )

    monkeypatch.setattr("skyvern.forge.agent.resolve_validation_evidence_route", fake_router)

    ctx = SkyvernContext(tz_info=None)
    token = skyvern_context._context.set(ctx)
    try:
        build_result = await patched_agent._build_extract_action_prompt(
            task,
            step,
            _make_browser_state(),
            _make_scraped_page(),
        )
    finally:
        skyvern_context._context.reset(token)

    assert build_result.without_page_information is False
    assert "data-skyvern" in build_result.prompt or "<div" in build_result.prompt


@pytest.mark.asyncio
async def test_non_validation_task_does_not_invoke_router(
    patched_agent: ForgeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only TaskType.validation should consult the router. General tasks must
    keep returning ``without_page_information=False`` and never call the
    router helper."""
    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(now, org, task_type=TaskType.general)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    sentinel = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.resolve_validation_evidence_route", sentinel)

    ctx = SkyvernContext(tz_info=None)
    token = skyvern_context._context.set(ctx)
    try:
        build_result = await patched_agent._build_extract_action_prompt(
            task,
            step,
            _make_browser_state(),
            _make_scraped_page(),
        )
    finally:
        skyvern_context._context.reset(token)

    without_page_information = build_result.without_page_information
    assert without_page_information is False
    sentinel.assert_not_awaited()
