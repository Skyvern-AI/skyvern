from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import litellm  # type: ignore[import-not-found]
import pytest  # type: ignore[import-not-found]

from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    EXTRACT_ACTION_PROMPT_NAME,
    LLMAPIHandlerFactory,
)
from skyvern.forge.sdk.api.llm.models import LLMConfig
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
