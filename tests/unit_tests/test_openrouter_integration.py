import importlib
import json
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern import config
from skyvern.config import Settings
from skyvern.forge import app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.sdk.api.llm import api_handler_factory, config_registry
from skyvern.forge.sdk.settings_manager import SettingsManager


@pytest.fixture(scope="module", autouse=True)
def setup_forge_app():
    start_forge_app()
    yield


class DummyResponse(dict):
    def __init__(self, content: str):
        super().__init__({"choices": [{"message": {"content": content}}], "usage": {}})
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]

    def model_dump_json(self, indent: int = 2):
        return json.dumps(self, indent=indent)

    def model_dump(self):
        return self


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
    # Update settings via monkeypatch to ensure config_registry sees them

    monkeypatch.setattr(config.settings, "ENABLE_OPENROUTER", True)
    monkeypatch.setattr(config.settings, "OPENROUTER_API_KEY", "key")
    monkeypatch.setattr(config.settings, "OPENROUTER_MODEL", "base-model")
    monkeypatch.setattr(config.settings, "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")

    # Clear existing configs before reload
    config_registry.LLMConfigRegistry._configs.clear()
    importlib.reload(config_registry)

    monkeypatch.setattr(app, "ARTIFACT_MANAGER", DummyArtifactManager())

    # Mock the AsyncOpenAI client
    async_mock = AsyncMock(return_value=DummyResponse('{"status": "ok"}'))
    mock_client = MagicMock()
    mock_client.chat.completions.create = async_mock

    # Patch AsyncOpenAI to return our mock client
    monkeypatch.setattr(api_handler_factory, "AsyncOpenAI", lambda **kwargs: mock_client)

    base_handler = api_handler_factory.LLMAPIHandlerFactory.get_llm_api_handler("OPENROUTER")
    override_handler = api_handler_factory.LLMAPIHandlerFactory.get_override_llm_api_handler(
        "openrouter/other-model", default=base_handler
    )

    result = await override_handler("hi", "test")
    assert result == {"status": "ok"}
    called_model = async_mock.call_args.kwargs.get("model")
    assert called_model == "other-model"


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
