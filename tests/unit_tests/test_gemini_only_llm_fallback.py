"""
Tests for SKY-50: Docker setup falls back to OPENAI_GPT4O despite Gemini-only env config.

Verifies that when a user configures a non-OpenAI provider (e.g. ENABLE_GEMINI=true)
without explicitly setting LLM_KEY, the system auto-resolves LLM_KEY to the first
enabled provider's default key instead of falling back to OPENAI_GPT4O.
"""

import importlib

from skyvern import config
from skyvern.forge.sdk.api.llm import config_registry


def _setup_gemini_only_env(monkeypatch):
    """Configure settings to simulate a Gemini-only Docker environment."""
    monkeypatch.setattr(config.settings, "ENABLE_OPENAI", False)
    monkeypatch.setattr(config.settings, "ENABLE_ANTHROPIC", False)
    monkeypatch.setattr(config.settings, "ENABLE_GEMINI", True)
    monkeypatch.setattr(config.settings, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(config.settings, "ENABLE_AZURE", False)
    monkeypatch.setattr(config.settings, "ENABLE_BEDROCK", False)
    monkeypatch.setattr(config.settings, "ENABLE_BEDROCK_ANTHROPIC", False)
    monkeypatch.setattr(config.settings, "ENABLE_OPENAI_COMPATIBLE", False)
    monkeypatch.setattr(config.settings, "ENABLE_OLLAMA", False)
    monkeypatch.setattr(config.settings, "ENABLE_GROQ", False)
    monkeypatch.setattr(config.settings, "ENABLE_VOLCENGINE", False)
    monkeypatch.setattr(config.settings, "ENABLE_OPENROUTER", False)
    monkeypatch.setattr(config.settings, "ENABLE_NOVITA", False)
    monkeypatch.setattr(config.settings, "ENABLE_MOONSHOT", False)
    monkeypatch.setattr(config.settings, "ENABLE_VERTEX_AI", False)


class TestLLMKeyAutoResolution:
    """Tests for the LLM_KEY auto-resolution logic in Settings._resolve_llm_key_default."""

    def test_resolves_to_gemini_when_only_gemini_enabled(self, monkeypatch):
        """When ENABLE_GEMINI=true and ENABLE_OPENAI=false, LLM_KEY should auto-resolve
        to GEMINI_FLASH_2_0 instead of staying at the OPENAI_GPT4O default."""
        _setup_gemini_only_env(monkeypatch)
        # Reset LLM_KEY to the default to simulate fresh startup
        monkeypatch.setattr(config.settings, "LLM_KEY", "OPENAI_GPT4O")

        config.settings._resolve_llm_key_default()

        assert config.settings.LLM_KEY == "GEMINI_FLASH_2_0"

    def test_resolves_to_anthropic_when_only_anthropic_enabled(self, monkeypatch):
        """When ENABLE_ANTHROPIC=true and ENABLE_OPENAI=false, LLM_KEY should auto-resolve
        to ANTHROPIC_CLAUDE3."""
        _setup_gemini_only_env(monkeypatch)
        monkeypatch.setattr(config.settings, "ENABLE_GEMINI", False)
        monkeypatch.setattr(config.settings, "ENABLE_ANTHROPIC", True)
        monkeypatch.setattr(config.settings, "LLM_KEY", "OPENAI_GPT4O")

        config.settings._resolve_llm_key_default()

        assert config.settings.LLM_KEY == "ANTHROPIC_CLAUDE3"

    def test_keeps_openai_default_when_openai_enabled(self, monkeypatch):
        """When ENABLE_OPENAI=true, LLM_KEY should stay as OPENAI_GPT4O."""
        monkeypatch.setattr(config.settings, "ENABLE_OPENAI", True)
        monkeypatch.setattr(config.settings, "LLM_KEY", "OPENAI_GPT4O")

        config.settings._resolve_llm_key_default()

        assert config.settings.LLM_KEY == "OPENAI_GPT4O"

    def test_keeps_explicit_llm_key(self, monkeypatch):
        """When LLM_KEY is explicitly set to a non-default value, it should be preserved."""
        _setup_gemini_only_env(monkeypatch)
        monkeypatch.setattr(config.settings, "LLM_KEY", "GEMINI_2.5_PRO")

        config.settings._resolve_llm_key_default()

        assert config.settings.LLM_KEY == "GEMINI_2.5_PRO"

    def test_keeps_custom_model_string(self, monkeypatch):
        """When LLM_KEY is set to a custom litellm model string, it should be preserved."""
        _setup_gemini_only_env(monkeypatch)
        monkeypatch.setattr(config.settings, "LLM_KEY", "gemini/gemini-2.0-flash")

        config.settings._resolve_llm_key_default()

        assert config.settings.LLM_KEY == "gemini/gemini-2.0-flash"


class TestGeminiOnlyEndToEnd:
    """End-to-end tests verifying that Gemini-only config resolves to a registered model."""

    def test_resolved_llm_key_is_registered(self, monkeypatch):
        """After auto-resolution, LLM_KEY should point to a registered model in the registry."""
        _setup_gemini_only_env(monkeypatch)
        monkeypatch.setattr(config.settings, "LLM_KEY", "OPENAI_GPT4O")

        config.settings._resolve_llm_key_default()

        # Reload config registry with Gemini-only settings
        importlib.reload(config_registry)
        registry = config_registry.LLMConfigRegistry

        registered_keys = registry.get_model_names()
        assert config.settings.LLM_KEY in registered_keys, (
            f"LLM_KEY='{config.settings.LLM_KEY}' is not among registered models {registered_keys}."
        )

    def test_resolved_config_uses_gemini_model_name(self, monkeypatch):
        """The resolved config should use a proper Gemini model name for litellm."""
        _setup_gemini_only_env(monkeypatch)
        monkeypatch.setattr(config.settings, "LLM_KEY", "OPENAI_GPT4O")

        config.settings._resolve_llm_key_default()

        # Reload config registry with Gemini-only settings
        importlib.reload(config_registry)
        registry = config_registry.LLMConfigRegistry

        llm_config = registry.get_config(config.settings.LLM_KEY)
        assert "gemini" in llm_config.model_name.lower(), (
            f"Expected a Gemini model name but got '{llm_config.model_name}'"
        )
