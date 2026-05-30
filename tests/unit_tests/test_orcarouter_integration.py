import importlib
import json
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern import config
from skyvern.forge import app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.sdk.api.llm import api_handler_factory, config_registry


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

    async def bulk_create_artifacts(self, *args, **kwargs):
        return None


def _apply_orcarouter_settings(monkeypatch, *, model: str = "anthropic/claude-sonnet-4.6") -> None:
    """Patch the live settings object so config_registry reload sees the new values.

    importlib.reload rebuilds the LLMConfigRegistry class, but api_handler_factory still
    holds a reference to the pre-reload class. Rebind it after reload so the handler
    factory and the test see the same registry instance.
    """
    monkeypatch.setattr(config.settings, "ENABLE_ORCAROUTER", True)
    monkeypatch.setattr(config.settings, "ORCAROUTER_API_KEY", "key")
    monkeypatch.setattr(config.settings, "ORCAROUTER_MODEL", model)
    monkeypatch.setattr(config.settings, "ORCAROUTER_API_BASE", "https://api.orcarouter.ai/v1")
    monkeypatch.setattr(config.settings, "LLM_KEY", "ORCAROUTER")

    config_registry.LLMConfigRegistry._configs.clear()
    importlib.reload(config_registry)
    monkeypatch.setattr(api_handler_factory, "LLMConfigRegistry", config_registry.LLMConfigRegistry)


def test_orcarouter_config_registration(monkeypatch):
    """ORCAROUTER registers with the raw model id — no LiteLLM provider prefix."""
    _apply_orcarouter_settings(monkeypatch, model="anthropic/claude-sonnet-4.6")

    cfg = config_registry.LLMConfigRegistry.get_config("ORCAROUTER")
    assert cfg.model_name == "anthropic/claude-sonnet-4.6"
    assert cfg.litellm_params["api_key"] == "key"
    assert cfg.litellm_params["api_base"] == "https://api.orcarouter.ai/v1"
    assert cfg.litellm_params["model_info"]["model_name"] == "anthropic/claude-sonnet-4.6"


def test_orcarouter_skipped_when_model_unset(monkeypatch):
    """Mirrors the OpenRouter/Groq guard: registration is skipped when ORCAROUTER_MODEL is empty."""
    monkeypatch.setattr(config.settings, "ENABLE_ORCAROUTER", True)
    monkeypatch.setattr(config.settings, "ORCAROUTER_API_KEY", "key")
    monkeypatch.setattr(config.settings, "ORCAROUTER_MODEL", "")
    monkeypatch.setattr(config.settings, "ORCAROUTER_API_BASE", "https://api.orcarouter.ai/v1")

    config_registry.LLMConfigRegistry._configs.clear()
    importlib.reload(config_registry)

    assert not config_registry.LLMConfigRegistry.is_registered("ORCAROUTER")


def test_orcarouter_llmcaller_bypasses_litellm(monkeypatch):
    """LLMCaller for ORCAROUTER should set up AsyncOpenAI against the OrcaRouter base URL
    and rewrite self.llm_key to the configured model id (no LiteLLM in the path)."""
    _apply_orcarouter_settings(monkeypatch, model="anthropic/claude-sonnet-4.6")

    captured_kwargs: dict = {}

    def _fake_async_openai(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(api_handler_factory, "AsyncOpenAI", _fake_async_openai)

    caller = api_handler_factory.LLMCaller(llm_key="ORCAROUTER")
    assert caller.openai_client is not None
    assert caller.llm_key == "anthropic/claude-sonnet-4.6"
    assert caller.original_llm_key == "ORCAROUTER"
    assert captured_kwargs["api_key"] == "key"
    assert captured_kwargs["base_url"] == "https://api.orcarouter.ai/v1"


@pytest.mark.asyncio
async def test_orcarouter_basic_completion(monkeypatch):
    """End-to-end: ORCAROUTER handler should hit the OrcaRouter AsyncOpenAI client with
    the configured model id, and never touch litellm.acompletion."""
    _apply_orcarouter_settings(monkeypatch)

    monkeypatch.setattr(app, "ARTIFACT_MANAGER", DummyArtifactManager())

    completions_mock = AsyncMock(return_value=DummyResponse('{"result": "ok"}'))
    mock_client = MagicMock()
    mock_client.chat.completions.create = completions_mock
    monkeypatch.setattr(api_handler_factory, "AsyncOpenAI", lambda **kwargs: mock_client)

    # Guard: litellm.acompletion must not be invoked on this path.
    sentinel_litellm = AsyncMock(side_effect=AssertionError("litellm.acompletion should not be called"))
    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", sentinel_litellm)

    handler = api_handler_factory.LLMAPIHandlerFactory.get_llm_api_handler("ORCAROUTER")
    result = await handler("hi", "test")

    assert result == {"result": "ok"}
    completions_mock.assert_called_once()
    sentinel_litellm.assert_not_called()
    assert completions_mock.call_args.kwargs["model"] == "anthropic/claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_orcarouter_error_propagation(monkeypatch):
    """Upstream API errors should surface as LLMProviderError, matching other LLMCaller paths."""
    _apply_orcarouter_settings(monkeypatch)

    monkeypatch.setattr(app, "ARTIFACT_MANAGER", DummyArtifactManager())

    class DummyAPIError(Exception):
        pass

    async def _raise(*args, **kwargs):
        raise DummyAPIError("boom")

    mock_client = MagicMock()
    mock_client.chat.completions.create = _raise
    monkeypatch.setattr(api_handler_factory, "AsyncOpenAI", lambda **kwargs: mock_client)

    handler = api_handler_factory.LLMAPIHandlerFactory.get_llm_api_handler("ORCAROUTER")
    with pytest.raises(api_handler_factory.LLMProviderError):
        await handler("hi", "test")
