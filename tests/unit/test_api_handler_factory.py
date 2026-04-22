from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest  # type: ignore[import-not-found]

from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    EXTRACT_ACTION_PROMPT_NAME,
    LLMAPIHandlerFactory,
)
from skyvern.forge.sdk.api.llm.models import LLMConfig
from skyvern.forge.sdk.models import Step, StepStatus
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


def test_assert_step_thought_exclusive_rejects_both_set() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        api_handler_factory._assert_step_thought_exclusive(MagicMock(), MagicMock())


def test_assert_step_thought_exclusive_allows_single_or_neither() -> None:
    api_handler_factory._assert_step_thought_exclusive(None, None)
    api_handler_factory._assert_step_thought_exclusive(MagicMock(), None)
    api_handler_factory._assert_step_thought_exclusive(None, MagicMock())


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
