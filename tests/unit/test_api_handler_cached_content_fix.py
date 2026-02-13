from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.forge as forge_module
import skyvern.forge.sdk.core.skyvern_context as skyvern_context_module
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.models import LLMConfig
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

# Replace the forge app holder with a MagicMock so test imports don't require a fully
# initialised ForgeApp instance.
forge_module.set_force_app_instance(MagicMock())
forge_module.app.EXPERIMENTATION_PROVIDER = MagicMock()


@pytest.mark.asyncio
async def test_cached_content_removed_from_non_extract_prompts() -> None:
    mock_config = MagicMock(spec=LLMConfig)
    mock_config.model_name = "gemini-2.5-pro"
    mock_config.litellm_params = {}
    mock_config.supports_vision = False
    mock_config.add_assistant_prefix = False
    mock_config.max_completion_tokens = 100
    mock_config.max_tokens = None
    mock_config.temperature = 0.0
    mock_config.reasoning_effort = None
    mock_config.disable_cooldowns = True

    mock_response = MagicMock()
    mock_response.model_dump_json.return_value = "{}"
    mock_response.choices = [MagicMock(message=MagicMock(content="test"))]
    mock_response.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=10,
        completion_tokens_details=None,
        prompt_tokens_details=None,
        cache_read_input_tokens=0,
    )

    # Ensure app dependencies referenced inside the handler resolve to async mocks.
    forge_module.app.ARTIFACT_MANAGER = MagicMock()
    forge_module.app.DATABASE = MagicMock()
    forge_module.app.DATABASE.update_step = AsyncMock()
    forge_module.app.DATABASE.update_thought = AsyncMock()

    with (
        patch("skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", return_value=mock_config),
        patch(
            "skyvern.forge.sdk.api.llm.api_handler_factory.litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_acompletion,
        patch(
            "skyvern.forge.sdk.api.llm.api_handler_factory.litellm.completion_cost",
            return_value=0.001,
        ),
        patch(
            "skyvern.forge.sdk.api.llm.api_handler_factory.llm_messages_builder", new_callable=AsyncMock
        ) as mock_builder,
        patch("skyvern.forge.sdk.api.llm.api_handler_factory.parse_api_response", return_value={}),
    ):
        mock_builder.return_value = [{"role": "user", "content": "test"}]
        mock_acompletion.return_value = mock_response

        handler = LLMAPIHandlerFactory.get_llm_api_handler("gemini-2.5-pro")

        context = SkyvernContext()
        context.cached_static_prompt = "some static prompt"
        context.use_prompt_caching = True
        context.vertex_cache_name = "projects/123/locations/global/cachedContents/demo"

        token = skyvern_context_module._context.set(context)
        try:
            # Extract actions attaches cached_content.
            await handler(prompt="test", prompt_name="extract-actions")
            args, kwargs = mock_acompletion.call_args
            assert kwargs.get("cached_content") == "projects/123/locations/global/cachedContents/demo"

            # Non-extract prompt should not include cached_content.
            mock_acompletion.reset_mock()
            await handler(prompt="test", prompt_name="check-user-goal")
            _, kwargs = mock_acompletion.call_args
            assert "cached_content" not in kwargs

            # Even if user supplied cached_content manually, it must be stripped.
            mock_acompletion.reset_mock()
            await handler(prompt="test", prompt_name="check-user-goal", parameters={"cached_content": "leaked"})
            _, kwargs = mock_acompletion.call_args
            assert "cached_content" not in kwargs
        finally:
            skyvern_context_module._context.reset(token)
