from __future__ import annotations

from unittest.mock import patch

import pytest

from skyvern.config import settings as base_settings
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.schemas.workflows import TextPromptBlockYAML, _get_text_prompt_model_name_by_llm_key


@pytest.fixture(autouse=True)
def _clear_llm_key_cache():
    """Clear the lru_cache before each test to prevent cross-test pollution."""
    _get_text_prompt_model_name_by_llm_key.cache_clear()
    yield
    _get_text_prompt_model_name_by_llm_key.cache_clear()


class TestTextPromptBlockYAMLNormalization:
    def test_converts_known_llm_key_to_model(self, monkeypatch) -> None:
        monkeypatch.setattr(SettingsManager, "_SettingsManager__instance", base_settings)

        block = TextPromptBlockYAML(
            label="summarize",
            prompt="Summarize the data.",
            llm_key="VERTEX_GEMINI_2.5_FLASH",
        )

        assert block.model == {"model_name": "gemini-2.5-flash"}
        assert block.llm_key is None

    def test_clears_invalid_llm_key_to_use_default_model(self, monkeypatch) -> None:
        monkeypatch.setattr(SettingsManager, "_SettingsManager__instance", base_settings)

        with patch(
            "skyvern.schemas.workflows.LLMConfigRegistry.get_model_names",
            return_value=[],
        ):
            block = TextPromptBlockYAML(
                label="summarize",
                prompt="Summarize the data.",
                llm_key="ANTHROPIC_CLAUDE_3_5_SONNET",
            )

        assert block.model is None
        assert block.llm_key is None

    def test_preserves_registered_advanced_llm_key(self, monkeypatch) -> None:
        monkeypatch.setattr(SettingsManager, "_SettingsManager__instance", base_settings)

        with patch(
            "skyvern.schemas.workflows.LLMConfigRegistry.get_model_names",
            return_value=["SPECIAL_INTERNAL_KEY"],
        ):
            block = TextPromptBlockYAML(
                label="summarize",
                prompt="Summarize the data.",
                llm_key="SPECIAL_INTERNAL_KEY",
            )

        assert block.model is None
        assert block.llm_key == "SPECIAL_INTERNAL_KEY"

    def test_preserves_templated_llm_key(self, monkeypatch) -> None:
        monkeypatch.setattr(SettingsManager, "_SettingsManager__instance", base_settings)

        block = TextPromptBlockYAML(
            label="summarize",
            prompt="Summarize the data.",
            llm_key="{{ prompt_block_llm_key }}",
        )

        assert block.model is None
        assert block.llm_key == "{{ prompt_block_llm_key }}"

    def test_model_override_clears_raw_llm_key(self, monkeypatch) -> None:
        monkeypatch.setattr(SettingsManager, "_SettingsManager__instance", base_settings)

        block = TextPromptBlockYAML(
            label="summarize",
            prompt="Summarize the data.",
            llm_key="ANTHROPIC_CLAUDE_3_5_SONNET",
            model={"model_name": "gemini-3-pro-preview"},
        )

        assert block.model == {"model_name": "gemini-3-pro-preview"}
        assert block.llm_key is None
