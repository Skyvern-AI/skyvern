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


@pytest.fixture(autouse=True)
def _isolate_llm_settings():
    """Every test below mutates three pieces of process-global state --
    SettingsManager's active settings instance, LLMConfigRegistry's
    registered configs, and config_registry's own module-level `settings`
    binding (rebound on every `importlib.reload`, and read directly by
    get_config()'s synthesis fallback) -- without restoring any of them,
    which can leak into (or be leaked into by) unrelated tests depending on
    execution order. Snapshot and restore all three around every test in
    this module.
    """
    previous_settings = SettingsManager.get_settings()
    # Shallow copy is safe: LLMConfig/LLMRouterConfig values are frozen dataclasses.
    previous_configs = dict(config_registry.LLMConfigRegistry._configs)
    previous_module_settings = config_registry.settings
    yield
    SettingsManager.set_settings(previous_settings)
    config_registry.LLMConfigRegistry._configs.clear()
    config_registry.LLMConfigRegistry._configs.update(previous_configs)
    config_registry.settings = previous_module_settings


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
    # config_registry's module-level registration reads skyvern.config.settings
    # directly, while LLMConfigBase.get_missing_env_vars() reads through
    # SettingsManager.get_settings() -- normally the same object, but any test
    # (including siblings in this file) that calls SettingsManager.set_settings()
    # without restoring it breaks that identity. Point both at one new object so
    # this test doesn't depend on which one happened to run last.
    settings = Settings(
        ENABLE_OPENROUTER=True,
        OPENROUTER_API_KEY="key",
        OPENROUTER_MODEL="base-model",
        OPENROUTER_API_BASE="https://openrouter.ai/api/v1",
        LLM_KEY="OPENROUTER",
    )
    SettingsManager.set_settings(settings)
    monkeypatch.setattr(config, "settings", settings)

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


def test_isolate_llm_settings_restores_module_level_settings_binding():
    """Regression: config_registry does `from skyvern.config import settings`, so
    importlib.reload(config_registry) while skyvern.config.settings is monkeypatched
    rebinds config_registry's own module-level `settings` name to that patched
    object. The monkeypatch fixture only restores skyvern.config.settings itself,
    not this separate binding, and get_config()'s synthesis fallback reads it
    directly -- a leak here silently affects any later test's synthesized configs.
    """
    # __wrapped__ recovers the undecorated generator function -- an implementation
    # detail of pytest's fixture wrapper, not public API, but stable since pytest 3.
    gen = _isolate_llm_settings.__wrapped__()
    next(gen)  # run the fixture's setup half, snapshotting current state

    original_module_settings = config_registry.settings
    fake_settings = Settings(
        ENABLE_OPENROUTER=True,
        OPENROUTER_API_KEY="leaked-key",
        OPENROUTER_MODEL="base-model",
        OPENROUTER_API_BASE="https://openrouter.ai/api/v1",
        LLM_KEY="OPENROUTER",
    )
    with pytest.MonkeyPatch.context() as mp:
        # Registration reads config_registry's module-level settings (patched below);
        # validate_config's get_missing_env_vars() reads through SettingsManager --
        # both must point at the same object or registration itself raises.
        SettingsManager.set_settings(fake_settings)
        mp.setattr(config, "settings", fake_settings)
        importlib.reload(config_registry)
        assert config_registry.settings is fake_settings  # sanity: reload really did rebind it

    with pytest.raises(StopIteration):
        next(gen)  # run the fixture's teardown half

    assert config_registry.settings is original_module_settings


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
