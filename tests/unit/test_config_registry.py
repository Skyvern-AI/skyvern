import runpy
from pathlib import Path
from typing import Any

import pytest

from skyvern.forge.sdk.api.llm import config_registry
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.schemas import llm as llm_schemas


def test_xai_grok_4_5_cost_override_uses_separate_output_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    registered_models: dict[str, dict[str, Any]] = {}
    monkeypatch.setattr(config_registry.litellm, "register_model", registered_models.update)

    config_registry._register_model_cost_overrides()

    model_info = registered_models[config_registry.XAI_GROK_4_5_MODEL]
    assert model_info["max_input_tokens"] == config_registry.XAI_GROK_4_5_CONTEXT_WINDOW
    assert model_info["max_output_tokens"] == config_registry.XAI_GROK_4_5_MAX_OUTPUT_TOKENS
    assert model_info["max_tokens"] == config_registry.XAI_GROK_4_5_MAX_OUTPUT_TOKENS


def test_xai_grok_4_5_config_uses_reasoning_completion_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_registry.settings, "XAI_REASONING_EFFORT", "high")

    llm_config = config_registry._build_xai_grok_4_5_config()
    parameters = LLMAPIHandlerFactory.get_api_parameters(llm_config)

    assert llm_config.reasoning_effort == "high"
    assert parameters["max_completion_tokens"] == config_registry.XAI_GROK_4_5_MAX_OUTPUT_TOKENS
    assert "max_tokens" not in parameters


def test_openrouter_deepseek_v4_flash_0731_registry_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_registry.settings, "ENABLE_OPENROUTER", True)
    monkeypatch.setattr(config_registry.settings, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm_schemas, "_settings", lambda: config_registry.settings)
    assert config_registry.__file__ is not None

    registry_namespace = runpy.run_path(str(Path(config_registry.__file__)))
    registry = registry_namespace["LLMConfigRegistry"]

    assert registry.is_registered("OPENROUTER_DEEPSEEK_V4_FLASH_0731")
    llm_config = registry.get_config("OPENROUTER_DEEPSEEK_V4_FLASH_0731")
    assert llm_config.model_name == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert llm_config.supports_vision is False
    assert llm_config.litellm_params is not None
    assert llm_config.litellm_params["extra_body"] == {
        "reasoning_effort": "high",
        "provider": {
            "order": ["cloudflare", "parasail"],
            "allow_fallbacks": False,
            "quantizations": ["fp8"],
        },
    }
