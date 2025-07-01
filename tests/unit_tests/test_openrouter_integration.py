import importlib
import json
import types
from unittest.mock import AsyncMock

import pytest

from skyvern.config import Settings
from skyvern.forge import app
from skyvern.forge.sdk.api.llm import api_handler_factory, config_registry
from skyvern.forge.sdk.settings_manager import SettingsManager


class DummyResponse(dict):
    def __init__(self, content: str):
        super().__init__({"choices": [{"message": {"content": content}}], "usage": {}})
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]

    def model_dump_json(self, indent: int = 2):
        return json.dumps(self, indent=indent)


class DummyArtifactManager:
    async def create_llm_artifact(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_openrouter_basic_completion(monkeypatch):
    settings = Settings(
        ENABLE_OPENROUTER=True,
        OPENROUTER_API_KEY="key",
        OPENROUTER_MODEL="test-model",
        LLM_KEY="OPENROUTER",
    )
    SettingsManager.set_settings(settings)
    importlib.reload(config_registry)

    monkeypatch.setattr(app, "ARTIFACT_MANAGER", DummyArtifactManager())

    async_mock = AsyncMock(return_value=DummyResponse('{"result": "ok"}'))
    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", async_mock)

    handler = api_handler_factory.LLMAPIHandlerFactory.get_llm_api_handler("OPENROUTER")
    result = await handler("hi", "test")
    assert result == {"result": "ok"}
    async_mock.assert_called_once()


@pytest.mark.asyncio
async def test_openrouter_dynamic_model(monkeypatch):
    settings = Settings(
        ENABLE_OPENROUTER=True,
        OPENROUTER_API_KEY="key",
        OPENROUTER_MODEL="base-model",
        LLM_KEY="OPENROUTER",
    )
    SettingsManager.set_settings(settings)
    importlib.reload(config_registry)

    monkeypatch.setattr(app, "ARTIFACT_MANAGER", DummyArtifactManager())
    async_mock = AsyncMock(return_value=DummyResponse('{"status": "ok"}'))
    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", async_mock)

    base_handler = api_handler_factory.LLMAPIHandlerFactory.get_llm_api_handler("OPENROUTER")
    override_handler = api_handler_factory.LLMAPIHandlerFactory.get_override_llm_api_handler(
        "openrouter/other-model", default=base_handler
    )
    result = await override_handler("hi", "test")
    assert result == {"status": "ok"}
    called_model = async_mock.call_args.kwargs.get("model")
    assert called_model == "openrouter/other-model"


@pytest.mark.asyncio
async def test_openrouter_error_propagation(monkeypatch):
    class DummyAPIError(Exception):
        pass

    settings = Settings(
        ENABLE_OPENROUTER=True,
        OPENROUTER_API_KEY="key",
        OPENROUTER_MODEL="test-model",
        LLM_KEY="OPENROUTER",
    )
    SettingsManager.set_settings(settings)
    importlib.reload(config_registry)

    monkeypatch.setattr(app, "ARTIFACT_MANAGER", DummyArtifactManager())

    async def _raise(*args, **kwargs):
        raise DummyAPIError()

    fake_litellm = types.SimpleNamespace(
        acompletion=_raise,
        exceptions=types.SimpleNamespace(APIError=DummyAPIError),
    )
    monkeypatch.setattr(api_handler_factory, "litellm", fake_litellm)

    handler = api_handler_factory.LLMAPIHandlerFactory.get_llm_api_handler("OPENROUTER")
    with pytest.raises(api_handler_factory.LLMProviderErrorRetryableTask):
        await handler("hi", "test")
