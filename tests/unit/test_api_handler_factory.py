from __future__ import annotations

from asyncio import CancelledError
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import litellm  # type: ignore[import-not-found]
import pytest  # type: ignore[import-not-found]

from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    EXTRACT_ACTION_PROMPT_NAME,
    GEMINI_SAFETY_SETTINGS,
    LLMAPIHandlerFactory,
)
from skyvern.forge.sdk.api.llm.models import LLMConfig
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.schemas.llm import LLMRouterConfig, LLMRouterModelConfig
from tests.unit.helpers import FakeLLMResponse


@pytest.mark.asyncio
async def test_cached_content_not_added_for_non_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that cached_content is NOT added to non-Gemini models like GPT-4."""
    # Setup context with caching enabled
    context = MagicMock()
    context.vertex_cache_name = "projects/123/locations/us-central1/cachedContents/456"
    context.use_prompt_caching = True
    context.cached_static_prompt = "some static prompt"
    context.hashed_href_map = {}

    # Setup non-Gemini config
    llm_config = LLMConfig(
        model_name="gpt-4",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )

    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False
    )
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current", lambda: context)
    monkeypatch.setattr(
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    # Mock litellm.acompletion to capture the parameters
    completion_params = {}

    async def mock_acompletion(*args, **kwargs):
        completion_params.update(kwargs)
        return FakeLLMResponse("gpt-4")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Get handler and call it
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    # Verify cached_content was NOT passed
    assert "cached_content" not in completion_params
    assert completion_params["model"] == "gpt-4"


@pytest.mark.asyncio
async def test_cached_content_added_for_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that cached_content IS added for Gemini models."""
    # Setup context with caching enabled
    context = MagicMock()
    context.vertex_cache_name = "projects/123/locations/us-central1/cachedContents/456"
    context.use_prompt_caching = True
    context.cached_static_prompt = "some static prompt"
    context.hashed_href_map = {}

    # Setup Gemini config
    llm_config = LLMConfig(
        model_name="gemini-1.5-pro",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )

    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False
    )
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current", lambda: context)
    monkeypatch.setattr(
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    # Mock litellm.acompletion to capture the parameters
    completion_params = {}

    async def mock_acompletion(*args, **kwargs):
        completion_params.update(kwargs)
        return FakeLLMResponse("gemini-1.5-pro")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Get handler and call it
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gemini-1.5-pro")
    await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    # Verify cached_content WAS passed
    assert "cached_content" in completion_params
    assert completion_params["cached_content"] == "projects/123/locations/us-central1/cachedContents/456"
    assert completion_params["model"] == "gemini-1.5-pro"


@pytest.mark.asyncio
async def test_openai_caching_not_injected_for_check_user_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that OpenAI context caching system message is NOT injected for check-user-goal prompts.

    This is a regression test for a bug where the extract-action-static.j2 prompt was being
    injected as a system message for ALL prompts on OpenAI models, causing the LLM to return
    CLICK actions when running check-user-goal (which should only return COMPLETE/TERMINATE).
    """
    # Setup context with caching enabled (simulating state after extract-action ran)
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = True
    context.cached_static_prompt = "This is the extract-action-static prompt content"
    context.hashed_href_map = {}

    # Setup OpenAI config (GPT-4)
    llm_config = LLMConfig(
        model_name="gpt-4",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )

    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False
    )
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current", lambda: context)

    # Capture messages passed to LLM
    captured_messages: list = []

    async def mock_llm_messages_builder(prompt, screenshots, add_assistant_prefix):
        return [{"role": "user", "content": prompt}]

    monkeypatch.setattr(api_handler_factory, "llm_messages_builder", mock_llm_messages_builder)
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    async def mock_acompletion(*args, **kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return FakeLLMResponse("gpt-4")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Get handler and call it with check-user-goal prompt (NOT extract-actions)
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    await handler(prompt="check-user-goal prompt content", prompt_name="check-user-goal")

    # Verify the cached_static_prompt was NOT injected as a system message
    # There should only be the user message, no system message with the cached content
    system_messages = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_messages) == 0, (
        f"Expected no system messages with cached content for check-user-goal, but found: {system_messages}"
    )


@pytest.mark.asyncio
async def test_openai_caching_injected_for_extract_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that OpenAI context caching system message IS injected for extract-actions prompts."""
    # Setup context with caching enabled
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = True
    context.cached_static_prompt = "This is the extract-action-static prompt content"
    context.hashed_href_map = {}

    # Setup OpenAI config (GPT-4)
    llm_config = LLMConfig(
        model_name="gpt-4",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )

    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False
    )
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current", lambda: context)

    # Capture messages passed to LLM
    captured_messages: list = []

    async def mock_llm_messages_builder(prompt, screenshots, add_assistant_prefix):
        return [{"role": "user", "content": prompt}]

    monkeypatch.setattr(api_handler_factory, "llm_messages_builder", mock_llm_messages_builder)
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    async def mock_acompletion(*args, **kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return FakeLLMResponse("gpt-4")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Get handler and call it with extract-actions prompt
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    await handler(prompt="extract-actions prompt content", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    # Verify the cached_static_prompt WAS injected as a system message
    system_messages = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_messages) == 1, (
        f"Expected 1 system message with cached content for extract-actions, "
        f"but found {len(system_messages)}: {system_messages}"
    )
    # Check the system message contains the cached content
    system_content = system_messages[0].get("content", [])
    assert any(part.get("text") == "This is the extract-action-static prompt content" for part in system_content), (
        f"System message should contain cached_static_prompt, got: {system_content}"
    )


def test_normalize_llm_model_strips_provider_prefix() -> None:
    """LiteLLM returns model names with provider prefixes; dbt expects the bare name."""
    assert api_handler_factory._normalize_llm_model("vertex_ai/gemini-2.5-flash") == "gemini-2.5-flash"
    assert api_handler_factory._normalize_llm_model("openai/gpt-4.1-mini") == "gpt-4.1-mini"
    assert api_handler_factory._normalize_llm_model("gpt-4") == "gpt-4"
    assert api_handler_factory._normalize_llm_model(None) is None


@pytest.mark.parametrize(
    "model_name",
    [
        "anthropic/claude-opus-4-7",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-fable-5",
        "anthropic-claude-opus-4-8",
        "anthropic-claude-fable-5",
    ],
)
def test_requires_adaptive_thinking_for_direct_anthropic_models(model_name: str) -> None:
    assert LLMAPIHandlerFactory.requires_adaptive_thinking(model_name) is True


@pytest.mark.parametrize(
    "model_name",
    [
        "bedrock/us.anthropic.claude-opus-4-8",
        "bedrock/us.anthropic.claude-fable-5",
        "anthropic/claude-sonnet-4-6",
        None,
    ],
)
def test_requires_adaptive_thinking_does_not_rewrite_other_providers(model_name: str | None) -> None:
    assert LLMAPIHandlerFactory.requires_adaptive_thinking(model_name) is False


@pytest.mark.parametrize(
    "model_name",
    [
        "anthropic/claude-opus-4-8",
        "anthropic/claude-fable-5",
    ],
)
def test_apply_anthropic_thinking_optimization_uses_adaptive_shape(model_name: str) -> None:
    llm_config = LLMConfig(
        model_name=model_name,
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    params: dict[str, Any] = {}

    LLMAPIHandlerFactory._apply_anthropic_thinking_optimization(
        params,
        new_budget=2048,
        llm_config=llm_config,
        prompt_name="workflow-copilot-request-policy",
    )

    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": LLMAPIHandlerFactory.ADAPTIVE_THINKING_EFFORT}


def test_assert_step_thought_block_exclusive_rejects_both_set() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        api_handler_factory._assert_step_thought_block_exclusive(MagicMock(), MagicMock(), None)


def test_assert_step_thought_block_exclusive_rejects_step_and_block() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        api_handler_factory._assert_step_thought_block_exclusive(MagicMock(), None, "wfb_123")


def test_assert_step_thought_block_exclusive_allows_single_or_neither() -> None:
    api_handler_factory._assert_step_thought_block_exclusive(None, None, None)
    api_handler_factory._assert_step_thought_block_exclusive(MagicMock(), None, None)
    api_handler_factory._assert_step_thought_block_exclusive(None, MagicMock(), None)
    api_handler_factory._assert_step_thought_block_exclusive(None, None, "wfb_123")


@pytest.mark.asyncio
async def test_handler_persists_response_model_not_router_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler must persist response.model (normalized), not the config key used to resolve the handler."""
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = False
    context.cached_static_prompt = None
    context.hashed_href_map = {}
    context.use_artifact_bundling = False
    context.workflow_run_id = None
    context.task_id = None

    llm_config = LLMConfig(
        model_name="GEMINI_2_5_FLASH_WITH_FALLBACK",  # router group name, not what response.model returns
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )

    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False
    )
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current", lambda: context)
    monkeypatch.setattr(
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.01)

    # LiteLLM returns the actual backing model with its provider prefix
    async def mock_acompletion(*args, **kwargs):
        return FakeLLMResponse("vertex_ai/gemini-2.5-flash")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Capture update_step kwargs to assert on the llm_model value
    captured_kwargs: dict = {}

    async def mock_update_step(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    artifact_manager = MagicMock()
    artifact_manager.prepare_llm_artifact = AsyncMock(return_value=None)
    artifact_manager.bulk_create_artifacts = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.app.ARTIFACT_MANAGER", artifact_manager)
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.app.DATABASE.tasks.update_step", mock_update_step
    )

    now = datetime.now()
    step = Step(
        created_at=now,
        modified_at=now,
        task_id="tsk_test",
        step_id="stp_test",
        status=StepStatus.running,
        order=0,
        is_last=False,
        retry_index=0,
        organization_id="org_test",
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler("GEMINI_2_5_FLASH_WITH_FALLBACK")
    await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME, step=step)

    # The persisted model should be the bare response.model, not the router group key
    assert captured_kwargs.get("last_llm_model") == "gemini-2.5-flash"


def test_aiohttp_transport_disabled_for_per_request_timeouts() -> None:
    """Importing the LLM package disables litellm's aiohttp transport so per-request `timeout` is honored."""
    assert litellm.disable_aiohttp_transport is True
    assert api_handler_factory.litellm.disable_aiohttp_transport is True


@pytest.mark.parametrize("override", [None, ""])
def test_get_override_llm_api_handler_treats_empty_as_no_override(
    override: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty override_llm_key must return default — not the dummy handler.

    Block models persist `llm_key=""` rather than NULL when the user hasn't picked a
    model; SKY-9674 narrowed the gate to `is None`, which routed those calls to
    `dummy_llm_api_handler` and broke text_prompt blocks on staging.
    """

    async def default_handler(*_: object, **__: object) -> dict[str, str]:
        return {"ok": True}

    monkeypatch.setattr(LLMAPIHandlerFactory, "_maybe_get_flex_handler", staticmethod(lambda _default: None))

    resolved = LLMAPIHandlerFactory.get_override_llm_api_handler(override, default=default_handler)
    assert resolved is default_handler


# ---------------------------------------------------------------------------
# SKY-9785: Gemini 3 reasoning_effort experiment
# ---------------------------------------------------------------------------


def _gemini_3_flash_router() -> LLMRouterConfig:
    return LLMRouterConfig(
        model_name="gemini-3.0-flash-gpt-5-fallback-router",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(
                model_name="vertex-gemini-3-flash-preview",
                litellm_params={"model": "vertex_ai/gemini-3-flash-preview"},
            ),
        ],
        main_model_group="vertex-gemini-3-flash-preview",
        fallback_model_group="gpt-5-fallback",
    )


def _gemini_2_5_flash_router() -> LLMRouterConfig:
    return LLMRouterConfig(
        model_name="gemini-2.5-flash-gpt-5-mini-fallback-router",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(
                model_name="vertex-gemini-2.5-flash",
                litellm_params={"model": "vertex_ai/gemini-2.5-flash"},
            ),
        ],
        main_model_group="vertex-gemini-2.5-flash",
        fallback_model_group="gpt-5-mini-fallback",
    )


class TestGemini3ReasoningEffortExperiment:
    """SKY-9785 experiment. Grouped under a class so the autouse reset doesn't
    couple unrelated tests in this module to the new class-level override."""

    @pytest.fixture(autouse=True)
    def _reset_gemini_3_override(self) -> Any:
        """Make sure the class-level override doesn't leak between tests."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(None)
        yield
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(None)

    def test_is_gemini_3_model_detects_router_primary(self) -> None:
        """Router primary `main_model_group` carries the gemini-3 substring."""
        assert LLMAPIHandlerFactory._is_gemini_3_model(_gemini_3_flash_router()) is True

    def test_is_gemini_3_model_rejects_gemini_2(self) -> None:
        assert LLMAPIHandlerFactory._is_gemini_3_model(_gemini_2_5_flash_router()) is False

    def test_is_gemini_3_model_detects_direct_config(self) -> None:
        cfg = LLMConfig(
            model_name="vertex_ai/gemini-3-flash-preview",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
        )
        assert LLMAPIHandlerFactory._is_gemini_3_model(cfg) is True

    def test_apply_gemini_thinking_optimization_uses_reasoning_effort_for_gemini_3(self) -> None:
        """With the override set, Gemini 3 calls switch to reasoning_effort and drop the
        legacy `thinking` payload that litellm silently discards for Gemini 3."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("medium")
        params: dict[str, Any] = {"max_completion_tokens": 65536}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params,
            new_budget=1024,
            llm_config=_gemini_3_flash_router(),
            prompt_name="extract-information-from-file-text",
        )
        assert params["reasoning_effort"] == "medium"
        assert "thinking" not in params

    def test_apply_gemini_thinking_optimization_strips_existing_thinking_for_gemini_3(self) -> None:
        """Sending both reasoning_effort and thinking_budget would 400 in litellm for
        Gemini 3 — the override path must clean up any stale `thinking` payload."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("low")
        params: dict[str, Any] = {"thinking": {"budget_tokens": 1024}}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_3_flash_router(), prompt_name="text-prompt"
        )
        assert params["reasoning_effort"] == "low"
        assert "thinking" not in params

    def test_apply_gemini_thinking_optimization_leaves_gemini_2_5_alone(self) -> None:
        """The experiment only rewrites Gemini 3 calls. Gemini 2.5 keeps the strict
        `thinking={budget_tokens:N}` path that Vertex 2.5 honors."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("low")
        params: dict[str, Any] = {}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_2_5_flash_router(), prompt_name="extract-actions"
        )
        assert "reasoning_effort" not in params
        assert params["thinking"]["budget_tokens"] == 1024

    def test_apply_gemini_thinking_optimization_control_leaves_gemini_3_alone(self) -> None:
        """Override unset (control arm) — Gemini 3 keeps today's behavior so we have a
        clean comparison baseline."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(None)
        params: dict[str, Any] = {}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_3_flash_router(), prompt_name="extract-actions"
        )
        assert "reasoning_effort" not in params
        assert params["thinking"]["budget_tokens"] == 1024

    @pytest.mark.parametrize("value", ["minimal", "low", "medium", "high", "MEDIUM", " low "])
    def test_set_gemini_3_reasoning_effort_override_accepts_valid_values(self, value: str) -> None:
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(value)
        assert LLMAPIHandlerFactory._gemini_3_reasoning_effort_override == value.strip().lower()

    @pytest.mark.parametrize("value", ["disable", "off", "high-er", 1024])
    def test_set_gemini_3_reasoning_effort_override_rejects_invalid_values(self, value: Any) -> None:
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(value)
        assert LLMAPIHandlerFactory._gemini_3_reasoning_effort_override is None

    def test_apply_gemini_thinking_optimization_overrides_when_thinking_level_pre_merged(self) -> None:
        """Single-handler path (api_handler_factory.py:1402-1404) merges
        `llm_config.litellm_params` into parameters before optimization runs. For
        Gemini 3 configs this lifts `thinking_level="minimal"` into parameters and
        would otherwise trigger the early-return guard and silently skip the
        override. The reorder makes the override fire first."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("medium")
        # Simulate post-merge state from the single-handler path.
        params: dict[str, Any] = {
            "max_completion_tokens": 65536,
            "thinking_level": "minimal",
            "thinking": {"budget_tokens": 1024},
        }
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_3_flash_router(), prompt_name="extract-actions"
        )
        assert params["reasoning_effort"] == "medium"
        assert "thinking_level" not in params
        assert "thinking" not in params

    def test_apply_gemini_thinking_optimization_keeps_guard_for_gemini_2_5_with_thinking_level(self) -> None:
        """Override is set, but model is Gemini 2.5 — the override doesn't apply,
        and the legacy thinking_level guard fires as before (preserves the historic
        behavior that Gemini 2.5 routes with a thinking_level config field never
        get a budget_tokens write)."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("medium")
        params: dict[str, Any] = {"thinking_level": "minimal"}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_2_5_flash_router(), prompt_name="extract-actions"
        )
        # Override didn't fire (not gemini-3), guard fired, nothing else changed.
        assert "reasoning_effort" not in params
        assert "thinking" not in params
        assert params["thinking_level"] == "minimal"


# SKY-10200 — runtime tests for the router timeout-precedence fix and per-hop
# fallback chain expansion. These complement the config-shape tests in
# tests/cloud/test_llm_router_fallback.py by pinning the api_handler_factory
# wiring: that the router is constructed with a default timeout, the call
# sites don't pass a per-call timeout kwarg (which would clobber per-deployment
# values), and the fallbacks list expands into per-hop entries.


def _make_three_tier_router_config(*, fallback_groups: list[str]) -> LLMRouterConfig:
    """Synthetic 3+ tier router config that doesn't depend on the cloud
    `LLMConfigRegistry` registration that's conditional on prod env vars."""
    deployments = [
        LLMRouterModelConfig(model_name="primary-group", litellm_params={"model": "openai/primary", "timeout": 60}),
    ] + [
        LLMRouterModelConfig(model_name=group, litellm_params={"model": f"openai/{group}", "timeout": 60})
        for group in fallback_groups
    ]
    return LLMRouterConfig(
        model_name="test-router",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        model_list=deployments,
        redis_host="localhost",
        redis_port=6379,
        redis_password="",
        main_model_group="primary-group",
        fallback_model_group=fallback_groups,
        routing_strategy="simple-shuffle",
        num_retries=0,
        disable_cooldowns=True,
        temperature=None,
    )


def _stub_for_router_test(monkeypatch: pytest.MonkeyPatch, *, llm_key: str, config: LLMRouterConfig) -> None:
    """Wire a synthetic LLMRouterConfig into the registry and bypass env-var
    validation. Mirrors `router_test_context` from tests/unit/helpers.py."""
    from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry  # local import

    monkeypatch.setattr(LLMConfigRegistry, "validate_config", classmethod(lambda cls, key, cfg: None))
    LLMConfigRegistry._configs.pop(llm_key, None)  # type: ignore[attr-defined]
    LLMConfigRegistry.register_config(llm_key, config)
    LLMAPIHandlerFactory._router_handler_cache.pop(llm_key, None)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda completion_response: 0.0)

    async def fake_llm_messages_builder(prompt, screenshots, add_assistant_prefix):
        return [{"role": "user", "content": prompt}]

    monkeypatch.setattr(api_handler_factory, "llm_messages_builder", fake_llm_messages_builder)


def test_router_constructor_receives_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Router constructor must receive `timeout=settings.LLM_CONFIG_TIMEOUT`
    so deployments without an explicit per-deployment timeout fall back to this
    Router-level default (third precedence level per litellm/router.py
    _get_non_stream_timeout). Pre-fix this was passed at the per-call site
    instead, clobbering per-deployment values. SKY-10200 CORR-1."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["fallback-a", "fallback-b"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_ROUTER_DEFAULT_TIMEOUT", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_ROUTER_DEFAULT_TIMEOUT")

    assert captured.get("timeout") == api_handler_factory.settings.LLM_CONFIG_TIMEOUT, (
        f"Router must be constructed with timeout=settings.LLM_CONFIG_TIMEOUT; got timeout={captured.get('timeout')!r}"
    )


def test_router_fallbacks_payload_expands_per_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """fallbacks=[{main: [a, b, c]}, {a: [b, c]}, {b: [c]}] — each non-terminal
    hop carries its own outgoing chain so secondary entry points (e.g.
    truncation retry at api_handler_factory.py:1119 which calls
    router.acompletion(model=fallback_groups[0])) also benefit from the
    remaining chain. SKY-10200 COMP-4."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["hop-a", "hop-b", "hop-c"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_FALLBACK_EXPANSION", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FALLBACK_EXPANSION")

    expected = [
        {"primary-group": ["hop-a", "hop-b", "hop-c"]},
        {"hop-a": ["hop-b", "hop-c"]},
        {"hop-b": ["hop-c"]},
    ]
    assert captured.get("fallbacks") == expected, (
        f"fallbacks payload must expand to per-hop entries; got {captured.get('fallbacks')!r}"
    )


def test_router_fallbacks_payload_single_hop_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """For a single-hop chain the expansion produces the same single-dict shape
    as the legacy payload — no behavior change for routers that don't have a
    deeper chain. SKY-10200 regression check."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["only-fallback"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_FALLBACK_SINGLE_HOP", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FALLBACK_SINGLE_HOP")

    assert captured.get("fallbacks") == [{"primary-group": ["only-fallback"]}], (
        f"single-hop fallbacks payload must match legacy single-dict shape; got {captured.get('fallbacks')!r}"
    )


def test_router_fallbacks_payload_empty_when_no_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """No fallback groups → empty fallbacks list. SKY-10200 regression check."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = LLMRouterConfig(
        model_name="test-router-no-fb",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(model_name="primary-group", litellm_params={"model": "openai/primary"}),
        ],
        redis_host="localhost",
        redis_port=6379,
        redis_password="",
        main_model_group="primary-group",
        fallback_model_group=None,
        routing_strategy="simple-shuffle",
        num_retries=0,
        disable_cooldowns=True,
        temperature=None,
    )
    _stub_for_router_test(monkeypatch, llm_key="TEST_FALLBACK_EMPTY", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FALLBACK_EMPTY")

    assert captured.get("fallbacks") == [], (
        f"no-fallback router must construct with empty fallbacks list; got {captured.get('fallbacks')!r}"
    )


@pytest.mark.asyncio
async def test_router_acompletion_does_not_pass_timeout_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler must NOT pass `timeout=` as a kwarg to router.acompletion;
    that would override per-deployment litellm_params['timeout'] per litellm
    precedence (litellm/router.py:_get_non_stream_timeout). SKY-10200 CORR-1."""

    captured_calls: list[dict[str, Any]] = []

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            self._main = kwargs.get("model_list", [{}])[0].get("model_name", "primary-group")

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            captured_calls.append({"model": model, "kwargs": dict(kwargs)})
            return FakeLLMResponse(model)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["hop-a", "hop-b"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_NO_TIMEOUT_KWARG", config=config)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_NO_TIMEOUT_KWARG")
    await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert captured_calls, "router.acompletion was never invoked"
    for call in captured_calls:
        assert "timeout" not in call["kwargs"], (
            f"router.acompletion must not receive timeout= kwarg (it overrides per-deployment timeout); got call={call}"
        )


@pytest.mark.asyncio
async def test_router_retries_content_filter_on_first_non_gemini_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini's non-configurable content filter blocks a PII-heavy prompt, returning a *valid*
    empty ModelResponse that litellm's exception-driven router fallback never recovers. Retrying
    on another Gemini tier hits the same block, so the handler must skip the Gemini
    standard-fallback and jump to the first NON-Gemini fallback group (SKY-11766)."""

    calls: list[str] = []
    fallback_kwargs: dict[str, Any] = {}

    class _FilterThenSucceedRouter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            calls.append(model)
            if len(calls) == 1:
                return FakeLLMResponse("gemini-3.1-flash-lite", content=None, finish_reason="content_filter")
            fallback_kwargs.update(kwargs)
            return FakeLLMResponse("gpt-5-fallback", content='{"actions": []}')

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _FilterThenSucceedRouter)

    config = _make_three_tier_router_config(fallback_groups=["vertex-gemini-standard-fallback", "gpt-5-fallback"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_CONTENT_FILTER_FALLBACK", config=config)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_CONTENT_FILTER_FALLBACK")
    result = await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert calls == ["primary-group", "gpt-5-fallback"], (
        "handler must skip the Gemini standard-fallback tier and retry the first non-Gemini "
        f"fallback after a content_filter response; got calls={calls}"
    )
    assert result == {"actions": []}
    # The non-Gemini fallback call must not carry Gemini's safety_settings param — Azure 400s on
    # it and the fallback dies. get_api_parameters keeps it off router configs (per-deployment
    # injection instead), so **parameters stays clean here. Regression guard for incident #646.
    assert "safety_settings" not in fallback_kwargs, (
        f"non-Gemini fallback call must not carry safety_settings; got {fallback_kwargs}"
    )


@pytest.mark.asyncio
async def test_router_does_not_retry_content_filter_without_non_gemini_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every fallback group is also Gemini there is no filter-free tier to escape to — the
    content_filter must surface as a parse failure, not loop or retry another Gemini (SKY-11766)."""
    from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat

    calls: list[str] = []

    class _AlwaysFilterRouter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            calls.append(model)
            return FakeLLMResponse("gemini-3.1-flash-lite", content=None, finish_reason="content_filter")

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _AlwaysFilterRouter)

    config = _make_three_tier_router_config(fallback_groups=["vertex-gemini-standard-fallback"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_CONTENT_FILTER_NO_NON_GEMINI", config=config)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_CONTENT_FILTER_NO_NON_GEMINI")
    with pytest.raises((EmptyLLMResponseError, InvalidLLMResponseFormat)):
        await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert calls == ["primary-group"], f"must not retry when there is no non-Gemini fallback; got calls={calls}"


@pytest.mark.asyncio
async def test_router_does_not_retry_content_filter_for_non_gemini_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch is scoped to Gemini's non-configurable filter. A content_filter from a
    non-Gemini model must not trigger the Gemini-specific fallback retry (SKY-11766)."""
    from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat

    calls: list[str] = []

    class _FilterRouter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            calls.append(model)
            return FakeLLMResponse("gpt-5", content=None, finish_reason="content_filter")

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _FilterRouter)

    config = _make_three_tier_router_config(fallback_groups=["gpt-5-mini-fallback"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_CONTENT_FILTER_NON_GEMINI_MODEL", config=config)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_CONTENT_FILTER_NON_GEMINI_MODEL")
    with pytest.raises((EmptyLLMResponseError, InvalidLLMResponseFormat)):
        await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert calls == ["primary-group"], f"non-Gemini content_filter must not trigger Gemini fallback; got calls={calls}"


class _FakeExperimentationProvider:
    def __init__(self, variant: Any = None, error: Exception | None = None) -> None:
        self.variant = variant
        self.error = error
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get_value_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> Any:
        self.calls.append((feature_name, distinct_id, properties))
        if self.error is not None:
            raise self.error
        return self.variant


def _stub_content_filter_rescue(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm_key: str,
    variant: Any,
    provider_error: Exception | None = None,
    rescue_handler: Any = None,
    factory_error: Exception | None = None,
    variant_registered: bool = True,
) -> tuple[_FakeExperimentationProvider, list[str]]:
    """Wire a Gemini router that always content-filters, plus the rescue flag plumbing."""
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

    calls: list[str] = []

    class _FilterThenSucceedRouter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            calls.append(model)
            if model == "primary-group":
                return FakeLLMResponse("gemini-3.1-flash-lite-preview", content=None, finish_reason="content_filter")
            return FakeLLMResponse("gpt-5-fallback", content='{"actions": ["non-gemini-fallback"]}')

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _FilterThenSucceedRouter)
    config = _make_three_tier_router_config(fallback_groups=["vertex-gemini-standard-fallback", "gpt-5-fallback"])
    _stub_for_router_test(monkeypatch, llm_key=llm_key, config=config)

    ctx = SkyvernContext(organization_id="o_test", workflow_run_id="wr_test", workflow_permanent_id="wpid_test")
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: ctx)

    provider = _FakeExperimentationProvider(variant=variant, error=provider_error)
    monkeypatch.setattr(api_handler_factory.app, "EXPERIMENTATION_PROVIDER", provider)
    monkeypatch.setattr(api_handler_factory, "_content_filter_rescue_handler_cache", {})
    monkeypatch.setattr(api_handler_factory, "_content_filter_rescue_logged", set())
    monkeypatch.setattr(
        api_handler_factory.LLMConfigRegistry,
        "is_registered",
        classmethod(lambda cls, llm_key: variant_registered),
    )

    def fake_factory(variant_key: str) -> Any:
        if factory_error is not None:
            raise factory_error
        assert rescue_handler is not None, f"unexpected handler init for variant {variant_key}"
        return rescue_handler

    monkeypatch.setattr(LLMAPIHandlerFactory, "get_llm_api_handler", staticmethod(fake_factory))
    return provider, calls


@pytest.mark.asyncio
async def test_content_filter_rescue_flag_routes_to_configured_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """With CONTENT_FILTER_RESCUE_LLM_NAME set, a Gemini content_filter block must be retried on
    the configured llm_key's handler instead of the hardcoded first non-Gemini fallback group,
    and the flag must be evaluated with run-level distinct_id + org/wpid properties (SKY-11766)."""
    captured: dict[str, Any] = {}

    async def fake_rescue_handler(*, prompt: str, prompt_name: str, **kwargs: Any) -> dict[str, Any]:
        captured["prompt"] = prompt
        captured["prompt_name"] = prompt_name
        captured.update(kwargs)
        return {"actions": ["rescued"]}

    provider, router_calls = _stub_content_filter_rescue(
        monkeypatch, llm_key="TEST_RESCUE_FLAG_SET", variant="RESCUE_KEY", rescue_handler=fake_rescue_handler
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_FLAG_SET")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal")

    assert result == {"actions": ["rescued"]}
    assert captured["prompt"] == '{"actions": []}'
    assert captured["prompt_name"] == "check-user-goal"
    assert "parameters" not in captured or captured["parameters"] is None, (
        "rescue handler must derive parameters from its own llm_config, not inherit Gemini's"
    )
    assert router_calls == ["primary-group"], (
        f"rescue must replace the in-router non-Gemini retry; got router calls={router_calls}"
    )
    assert provider.calls, "rescue flag was never evaluated"
    feature_name, distinct_id, properties = provider.calls[0]
    assert feature_name == "CONTENT_FILTER_RESCUE_LLM_NAME"
    assert distinct_id == "wr_test"
    assert properties == {"organization_id": "o_test", "workflow_permanent_id": "wpid_test"}


@pytest.mark.asyncio
async def test_content_filter_rescue_flag_unset_keeps_non_gemini_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag disabled (provider returns None) → exact pre-flag behavior: retry on the first
    non-Gemini fallback group in the same router."""
    _provider, router_calls = _stub_content_filter_rescue(monkeypatch, llm_key="TEST_RESCUE_FLAG_UNSET", variant=None)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_FLAG_UNSET")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal")

    assert result == {"actions": ["non-gemini-fallback"]}
    assert router_calls == ["primary-group", "gpt-5-fallback"]


@pytest.mark.asyncio
async def test_content_filter_rescue_variant_matching_blocked_key_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Variant pointing at the llm_key that just got blocked would recurse forever — it must be
    ignored in favor of the default non-Gemini retry."""
    _provider, router_calls = _stub_content_filter_rescue(
        monkeypatch, llm_key="TEST_RESCUE_SELF_KEY", variant="TEST_RESCUE_SELF_KEY"
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_SELF_KEY")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal")

    assert result == {"actions": ["non-gemini-fallback"]}
    assert router_calls == ["primary-group", "gpt-5-fallback"]


@pytest.mark.asyncio
async def test_content_filter_rescue_provider_failure_keeps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag resolution failing (PostHog down) must not change behavior."""
    _provider, router_calls = _stub_content_filter_rescue(
        monkeypatch,
        llm_key="TEST_RESCUE_PROVIDER_DOWN",
        variant="RESCUE_KEY",
        provider_error=RuntimeError("posthog down"),
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_PROVIDER_DOWN")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal")

    assert result == {"actions": ["non-gemini-fallback"]}
    assert router_calls == ["primary-group", "gpt-5-fallback"]


@pytest.mark.asyncio
async def test_content_filter_rescue_unregistered_variant_never_builds_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_config synthesizes a config for unknown keys (the typo'd variant would reach litellm
    as a model name), so an unregistered variant must be rejected before any handler is built."""
    _provider, router_calls = _stub_content_filter_rescue(
        monkeypatch,
        llm_key="TEST_RESCUE_TYPO_VARIANT",
        variant="GEMINI_2_5_FLASH_LITE_WITH_FALBACK",
        variant_registered=False,
    )
    factory_calls: list[str] = []

    def recording_factory(variant_key: str) -> Any:
        factory_calls.append(variant_key)
        raise AssertionError("handler must not be constructed for an unregistered variant")

    monkeypatch.setattr(LLMAPIHandlerFactory, "get_llm_api_handler", staticmethod(recording_factory))

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_TYPO_VARIANT")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal")

    assert result == {"actions": ["non-gemini-fallback"]}
    assert router_calls == ["primary-group", "gpt-5-fallback"]
    assert factory_calls == [], "unregistered variant must fail closed at resolve time, not call time"


@pytest.mark.asyncio
async def test_content_filter_rescue_custom_llm_variant_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom LLMs are org-owned BYO endpoints registered under global CUSTOM_LLM_* keys with no
    ownership check on this path — a variant naming one must be rejected even though it IS a
    registered llm_key, before any handler is constructed or cached."""
    _provider, router_calls = _stub_content_filter_rescue(
        monkeypatch,
        llm_key="TEST_RESCUE_CUSTOM_VARIANT",
        variant="CUSTOM_LLM_cllm_123",
        variant_registered=True,
    )
    factory_calls: list[str] = []

    def recording_factory(variant_key: str) -> Any:
        factory_calls.append(variant_key)
        raise AssertionError("handler must not be constructed for a custom-LLM variant")

    monkeypatch.setattr(LLMAPIHandlerFactory, "get_llm_api_handler", staticmethod(recording_factory))

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_CUSTOM_VARIANT")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal")

    assert result == {"actions": ["non-gemini-fallback"]}
    assert router_calls == ["primary-group", "gpt-5-fallback"]
    assert factory_calls == [], "custom-LLM variant must never reach handler construction"


@pytest.mark.asyncio
async def test_content_filter_rescue_persists_blocked_response_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rescued early return must pair the blocked content_filter response with the
    already-recorded request instead of orphaning it — the blocked response is the debugging
    evidence for why the rescue fired."""

    async def fake_rescue_handler(*, prompt: str, prompt_name: str, **kwargs: Any) -> dict[str, Any]:
        return {"actions": ["rescued"]}

    _provider, _router_calls = _stub_content_filter_rescue(
        monkeypatch, llm_key="TEST_RESCUE_ARTIFACTS", variant="RESCUE_KEY", rescue_handler=fake_rescue_handler
    )
    artifact_manager = MagicMock()
    artifact_manager.prepare_llm_artifact = AsyncMock(return_value=None)
    artifact_manager.bulk_create_artifacts = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.app.ARTIFACT_MANAGER", artifact_manager)
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.app.DATABASE.tasks.update_step",
        AsyncMock(return_value=MagicMock()),
    )

    now = datetime.now()
    step = Step(
        created_at=now,
        modified_at=now,
        task_id="tsk_test",
        step_id="stp_test",
        status=StepStatus.running,
        order=0,
        is_last=False,
        retry_index=0,
        organization_id="o_test",
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_ARTIFACTS")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal", step=step)

    assert result == {"actions": ["rescued"]}
    persisted_types = [
        call.kwargs.get("artifact_type") for call in artifact_manager.prepare_llm_artifact.await_args_list
    ]
    assert ArtifactType.LLM_RESPONSE in persisted_types, (
        "blocked content_filter response must be persisted on the rescued path"
    )


@pytest.mark.asyncio
async def test_content_filter_rescue_handler_init_failure_keeps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered variant whose handler construction fails (e.g. missing provider env vars)
    must fall back to default behavior, not crash."""
    _provider, router_calls = _stub_content_filter_rescue(
        monkeypatch,
        llm_key="TEST_RESCUE_BAD_VARIANT",
        variant="RESCUE_KEY_BAD_CONFIG",
        factory_error=KeyError("RESCUE_KEY_BAD_CONFIG"),
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_BAD_VARIANT")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal")

    assert result == {"actions": ["non-gemini-fallback"]}
    assert router_calls == ["primary-group", "gpt-5-fallback"]


@pytest.mark.asyncio
async def test_content_filter_rescue_handler_failure_falls_back_to_non_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the rescue call itself fails, degrade to the default non-Gemini retry instead of
    failing the whole LLM call."""

    async def broken_rescue_handler(*, prompt: str, prompt_name: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("rescue provider outage")

    _provider, router_calls = _stub_content_filter_rescue(
        monkeypatch, llm_key="TEST_RESCUE_HANDLER_DOWN", variant="RESCUE_KEY", rescue_handler=broken_rescue_handler
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_RESCUE_HANDLER_DOWN")
    result = await handler(prompt='{"actions": []}', prompt_name="check-user-goal")

    assert result == {"actions": ["non-gemini-fallback"]}
    assert router_calls == ["primary-group", "gpt-5-fallback"]


def test_router_fallback_chain_no_duplicate_keys_or_overlapping_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-hop fallback expansion is constructed as strict suffixes — each
    non-terminal hop appears as a key at most once and each entry's chain
    drops one head from the parent. Together these prevent litellm from
    retrying the same hop more than once in a single request. SKY-10200."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["hop-a", "hop-b", "hop-c"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_NO_DOUBLE_INVOCATION", config=config)
    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_NO_DOUBLE_INVOCATION")

    fallbacks = captured.get("fallbacks", [])
    assert fallbacks, "fallbacks payload must be non-empty for a multi-hop chain"

    keys = [next(iter(entry.keys())) for entry in fallbacks]
    assert len(keys) == len(set(keys)), (
        f"each non-terminal hop must appear as a key at most once; got duplicates in {keys}. "
        "A repeated key would cause litellm to retry the same chain twice from that hop."
    )

    chains = [list(entry.values())[0] for entry in fallbacks]
    for i in range(1, len(chains)):
        assert chains[i] == chains[i - 1][1:], (
            f"each chain must drop one head from the previous (strict suffix); got chains={chains}. "
            "Non-suffix expansion could re-list already-tried hops and amplify retries."
        )


def test_completion_cost_halves_vertex_flex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vertex flex responses (trafficType ON_DEMAND_FLEX) bill at 50%: litellm reports
    them at the standard rate, so the helper applies the flex discount itself."""
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.10)

    flex = SimpleNamespace(_hidden_params={"provider_specific_fields": {"traffic_type": "ON_DEMAND_FLEX"}})
    standard = SimpleNamespace(_hidden_params={"provider_specific_fields": {"traffic_type": "ON_DEMAND"}})
    no_meta = SimpleNamespace(_hidden_params={})

    assert LLMAPIHandlerFactory._completion_cost(flex) == pytest.approx(0.05)
    assert LLMAPIHandlerFactory._completion_cost(standard) == pytest.approx(0.10)
    assert LLMAPIHandlerFactory._completion_cost(no_meta) == pytest.approx(0.10)


def test_completion_cost_returns_zero_when_litellm_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(completion_response: Any) -> float:
        raise RuntimeError("provider unsupported")

    monkeypatch.setattr(litellm, "completion_cost", _raise)
    resp = SimpleNamespace(_hidden_params={"provider_specific_fields": {"traffic_type": "ON_DEMAND_FLEX"}})
    assert LLMAPIHandlerFactory._completion_cost(resp) == 0.0


@pytest.mark.asyncio
async def test_non_speculative_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run-level cancellation (elapsed-time timeout / user stop) landing inside an LLM call must
    propagate as CancelledError so the timeout actually halts the run. It must NOT be converted into
    a retryable LLMProviderError, which the step loop would treat as a failure and retry."""
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = False
    context.cached_static_prompt = None
    context.hashed_href_map = {}

    llm_config = LLMConfig(
        model_name="gpt-4",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False
    )
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current", lambda: context)
    monkeypatch.setattr(
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)
    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=CancelledError()))

    # No step is passed, so is_speculative_step is False (the non-speculative branch).
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    with pytest.raises(BaseException) as exc_info:
        await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)
    assert isinstance(exc_info.value, CancelledError), (
        f"expected CancelledError to propagate, got {type(exc_info.value).__name__}"
    )


def test_get_api_parameters_injects_safety_settings_for_gemini_direct_config() -> None:
    llm_config = LLMConfig(
        model_name="vertex_ai/gemini-2.5-flash",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    params = LLMAPIHandlerFactory.get_api_parameters(llm_config)
    assert params["safety_settings"] == GEMINI_SAFETY_SETTINGS
    assert all(setting["threshold"] == "BLOCK_NONE" for setting in params["safety_settings"])


def test_get_api_parameters_omits_safety_settings_for_gemini_router_config() -> None:
    # Router configs must NOT carry safety_settings at the request level — it would ride along
    # to the non-Gemini fallback deployment and 400. Injection happens per-deployment instead.
    params = LLMAPIHandlerFactory.get_api_parameters(_gemini_2_5_flash_router())
    assert "safety_settings" not in params


def test_inject_gemini_safety_settings_targets_only_gemini_deployments() -> None:
    # Reproduces incident #646: a Gemini primary + Azure fallback in one router. safety_settings
    # must land on the Gemini deployment and stay off the Azure one so the fallback hop survives.
    model_list = [
        {"model_name": "vertex-gemini-2.5-flash-lite", "litellm_params": {"model": "vertex_ai/gemini-2.5-flash-lite"}},
        {"model_name": "gpt-5-mini-fallback", "litellm_params": {"model": "azure/gpt-5-mini"}},
    ]
    result = api_handler_factory._inject_gemini_safety_settings(model_list)
    assert result[0]["litellm_params"]["safety_settings"] == GEMINI_SAFETY_SETTINGS
    assert "safety_settings" not in result[1]["litellm_params"]


def test_get_api_parameters_omits_safety_settings_for_non_gemini_config() -> None:
    llm_config = LLMConfig(
        model_name="gpt-4",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    params = LLMAPIHandlerFactory.get_api_parameters(llm_config)
    assert "safety_settings" not in params
