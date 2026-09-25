import dataclasses
import importlib
from collections.abc import Iterator

import pytest

from skyvern import config
from skyvern.config import Settings
from skyvern.forge.sdk.api.llm import config_registry
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.schemas.llm import LLMConfig


@pytest.fixture
def gemini_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """ENABLE_GEMINI is off by default, so the Gemini block only registers on a reload with it
    set. The registry dict, config_registry's module-level `settings` binding and SettingsManager
    are all process-global; snapshot and restore the three so the reload can't leak into other
    tests (or be leaked into, depending on execution order).
    """
    previous_settings = SettingsManager.get_settings()
    previous_configs = dict(config_registry.LLMConfigRegistry._configs)
    previous_module_settings = config_registry.settings

    gemini_settings = Settings(ENABLE_GEMINI=True, GEMINI_API_KEY="test-key")
    SettingsManager.set_settings(gemini_settings)
    monkeypatch.setattr(config, "settings", gemini_settings)
    config_registry.LLMConfigRegistry._configs.clear()
    importlib.reload(config_registry)
    try:
        yield
    finally:
        SettingsManager.set_settings(previous_settings)
        config_registry.LLMConfigRegistry._configs.clear()
        config_registry.LLMConfigRegistry._configs.update(previous_configs)
        config_registry.settings = previous_module_settings


def test_gemini_3_1_flash_lite_flex_requests_the_flex_service_tier(gemini_registry: None) -> None:
    assert config_registry.LLMConfigRegistry.is_registered("GEMINI_3.1_FLASH_LITE_FLEX")

    flex = config_registry.LLMConfigRegistry.get_config("GEMINI_3.1_FLASH_LITE_FLEX")
    assert isinstance(flex, LLMConfig)
    assert flex.model_name == "gemini/gemini-3.1-flash-lite"
    assert flex.required_env_vars == ["GEMINI_API_KEY"]
    assert flex.litellm_params is not None
    assert flex.litellm_params["service_tier"] == "flex"
    assert flex.litellm_params["timeout"] == config_registry.FLEX_EXECUTION_TIMEOUT_SECONDS


def test_gemini_3_1_flash_lite_flex_differs_from_standard_only_by_tier(gemini_registry: None) -> None:
    """The flex twin must stay a pure tier swap: any other divergence (model id, token ceiling,
    thinking level) would make flex and standard runs silently non-comparable."""
    flex = config_registry.LLMConfigRegistry.get_config("GEMINI_3.1_FLASH_LITE_FLEX")
    standard = config_registry.LLMConfigRegistry.get_config("GEMINI_3.1_FLASH_LITE")
    assert isinstance(flex, LLMConfig)
    assert isinstance(standard, LLMConfig)

    flex_fields = dataclasses.asdict(flex)
    standard_fields = dataclasses.asdict(standard)
    assert flex_fields.pop("litellm_params") is not None
    assert standard_fields.pop("litellm_params") is not None
    assert flex_fields == standard_fields

    assert flex.litellm_params is not None
    assert standard.litellm_params is not None
    tier_only = {k: v for k, v in flex.litellm_params.items() if k not in ("service_tier", "timeout")}
    assert tier_only == dict(standard.litellm_params)
