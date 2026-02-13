from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.agent import (
    EXTRACT_ACTION_PROMPT_NAME,
    EXTRACT_ACTION_TEMPLATE,
    ForgeAgent,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext


@pytest.mark.asyncio
async def test_prompt_caching_settings_respect_experiment(monkeypatch):
    agent = ForgeAgent()
    context = SkyvernContext(run_id="wr_123", organization_id="org_456")
    mock_provider = AsyncMock()
    mock_provider.is_feature_enabled_cached.return_value = True
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", mock_provider)
    try:
        LLMAPIHandlerFactory.set_prompt_caching_settings(None)

        settings = await agent._get_prompt_caching_settings(context)

        assert settings == {
            EXTRACT_ACTION_PROMPT_NAME: True,
            EXTRACT_ACTION_TEMPLATE: True,
        }
        mock_provider.is_feature_enabled_cached.assert_awaited_once_with(
            "PROMPT_CACHING_OPTIMIZATION",
            "wr_123",
            properties={"organization_id": "org_456"},
        )

        # Cached on context; no second provider call
        await agent._get_prompt_caching_settings(context)
        assert mock_provider.is_feature_enabled_cached.await_count == 1
    finally:
        LLMAPIHandlerFactory.set_prompt_caching_settings(None)


@pytest.mark.asyncio
async def test_prompt_caching_settings_use_override(monkeypatch):
    agent = ForgeAgent()
    context = SkyvernContext(run_id="wr_789", organization_id="org_987")
    mock_provider = AsyncMock()
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", mock_provider)
    try:
        LLMAPIHandlerFactory.set_prompt_caching_settings({"extract-actions": True})

        settings = await agent._get_prompt_caching_settings(context)

        assert settings == {"extract-actions": True}
        mock_provider.is_feature_enabled_cached.assert_not_called()
    finally:
        LLMAPIHandlerFactory.set_prompt_caching_settings(None)
