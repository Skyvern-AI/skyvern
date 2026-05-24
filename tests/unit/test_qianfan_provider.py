import importlib

import pytest

from skyvern import config
from skyvern.config import Settings
from skyvern.forge.sdk.api.llm import config_registry
from skyvern.forge.sdk.api.llm.exceptions import MissingLLMProviderEnvVarsError
from skyvern.forge.sdk.settings_manager import SettingsManager


@pytest.fixture(autouse=True)
def restore_settings_and_registry():
    original_settings = config.settings
    yield
    SettingsManager.set_settings(original_settings)
    config.settings = original_settings
    config_registry.LLMConfigRegistry._configs.clear()
    importlib.reload(config_registry)


def _reload_registry(settings: Settings):
    SettingsManager.set_settings(settings)
    config.settings = settings
    config_registry.LLMConfigRegistry._configs.clear()
    return importlib.reload(config_registry)


def test_qianfan_settings_defaults_to_openai_compatible_international_endpoint() -> None:
    settings = Settings()

    assert settings.ENABLE_QIANFAN is False
    assert settings.QIANFAN_API_KEY is None
    assert settings.QIANFAN_API_BASE == "https://api.baiduqianfan.ai/v1"


def test_qianfan_registers_expected_ernie_models() -> None:
    registry_module = _reload_registry(
        Settings(
            ENABLE_QIANFAN=True,
            QIANFAN_API_KEY="test-key",
            QIANFAN_API_BASE="https://api.baiduqianfan.ai/v1",
        )
    )

    expected = {
        "QIANFAN_ERNIE_5_1": ("openai/ernie-5.1", False, 65536),
        "QIANFAN_ERNIE_5_0": ("openai/ernie-5.0", True, 65536),
        "QIANFAN_ERNIE_4_5_TURBO_128K": ("openai/ernie-4.5-turbo-128k", False, 12288),
        "QIANFAN_ERNIE_4_5_TURBO_VL": ("openai/ernie-4.5-turbo-vl", True, 16384),
    }

    for llm_key, (model_name, supports_vision, max_tokens) in expected.items():
        llm_config = registry_module.LLMConfigRegistry.get_config(llm_key)

        assert llm_config.model_name == model_name
        assert llm_config.required_env_vars == ["QIANFAN_API_KEY"]
        assert llm_config.supports_vision is supports_vision
        assert llm_config.add_assistant_prefix is False
        assert llm_config.max_completion_tokens == max_tokens
        assert llm_config.litellm_params == {
            "api_key": "test-key",
            "api_base": "https://api.baiduqianfan.ai/v1",
            "api_version": None,
            "model_info": {"model_name": model_name},
        }


def test_qianfan_registration_requires_api_key() -> None:
    settings = Settings(ENABLE_QIANFAN=True, QIANFAN_API_KEY=None)

    SettingsManager.set_settings(settings)
    config.settings = settings
    config_registry.LLMConfigRegistry._configs.clear()

    try:
        importlib.reload(config_registry)
    except MissingLLMProviderEnvVarsError as exc:
        assert "QIANFAN_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected Qianfan registration to require QIANFAN_API_KEY")
