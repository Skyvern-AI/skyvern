"""
Tests for Vertex AI cache model name extraction from LLMRouterConfig.

This tests the fix for the issue where GEMINI_3_0_FLASH_WITH_FALLBACK was
incorrectly using 'gemini-3.0-flash' instead of 'gemini-3-flash-preview'.
"""

from dataclasses import dataclass


@dataclass
class MockLLMRouterModelConfig:
    model_name: str
    litellm_params: dict


@dataclass
class MockLLMRouterConfig:
    model_name: str
    model_list: list
    main_model_group: str
    required_env_vars: list = None

    def __post_init__(self):
        if self.required_env_vars is None:
            self.required_env_vars = []


@dataclass
class MockLLMConfig:
    model_name: str
    required_env_vars: list = None
    litellm_params: dict = None

    def __post_init__(self):
        if self.required_env_vars is None:
            self.required_env_vars = []


class TestVertexCacheModelExtraction:
    """Test that model names are correctly extracted for Vertex AI caching."""

    def _extract_model_name(self, llm_config, resolved_llm_key: str) -> str:
        """
        Mimics the model name extraction logic from _create_vertex_cache_for_task.
        """
        import re

        model_name = "gemini-2.5-flash"  # Default
        extracted_name = None

        # For router configs (LLMRouterConfig), extract from model_list primary model FIRST
        # This must be checked before model_name since router model_name is just an identifier
        # (e.g., "gemini-3.0-flash-gpt-5-mini-fallback-router"), not an actual Vertex model
        if hasattr(llm_config, "model_list") and hasattr(llm_config, "main_model_group"):
            # Find the primary model in model_list by matching main_model_group
            for model_entry in llm_config.model_list:
                if model_entry.model_name == llm_config.main_model_group:
                    # Extract actual model name from litellm_params
                    model_param = model_entry.litellm_params.get("model", "")
                    if "vertex_ai/" in model_param:
                        extracted_name = model_param.split("/")[-1]
                    elif model_param.startswith("gemini-"):
                        extracted_name = model_param
                    break

        # Try to extract from model_name if it contains "vertex_ai/" or starts with "gemini-"
        if not extracted_name and hasattr(llm_config, "model_name") and isinstance(llm_config.model_name, str):
            if "vertex_ai/" in llm_config.model_name:
                # Direct Vertex config: "vertex_ai/gemini-2.5-flash" -> "gemini-2.5-flash"
                extracted_name = llm_config.model_name.split("/")[-1]
            elif llm_config.model_name.startswith("gemini-"):
                # Already in correct format
                extracted_name = llm_config.model_name

        # For router/fallback configs, extract from api_base or infer from key name
        if not extracted_name and hasattr(llm_config, "litellm_params") and llm_config.litellm_params:
            params = llm_config.litellm_params
            api_base = params.get("api_base") if isinstance(params, dict) else getattr(params, "api_base", None)
            if api_base and isinstance(api_base, str) and "/models/" in api_base:
                # Extract from URL: .../models/gemini-2.5-flash -> "gemini-2.5-flash"
                extracted_name = api_base.split("/models/")[-1]

        # For router configs without api_base, infer from the llm_key itself
        if not extracted_name:
            # Extract version from llm_key
            version_match = re.search(r"GEMINI[_-](\d+[._-]\d+)", resolved_llm_key, re.IGNORECASE)
            version = version_match.group(1).replace("_", ".").replace("-", ".") if version_match else "2.5"

            # Determine flavor
            if "_PRO_" in resolved_llm_key or resolved_llm_key.endswith("_PRO"):
                extracted_name = f"gemini-{version}-pro"
            elif "_FLASH_LITE_" in resolved_llm_key or resolved_llm_key.endswith("_FLASH_LITE"):
                extracted_name = f"gemini-{version}-flash-lite"
            else:
                # Default to flash flavor
                extracted_name = f"gemini-{version}-flash"

        if extracted_name:
            model_name = extracted_name

        # Normalize model name to the canonical Vertex identifier
        # Preserve preview suffixes so we don't strip required identifiers (e.g., gemini-3-flash-preview).
        match = re.search(r"(gemini-\d+(?:\.\d+)?-(?:flash-lite|flash|pro)(?:-preview)?)", model_name, re.IGNORECASE)
        if match:
            model_name = match.group(1).lower()

        return model_name

    def test_router_config_extracts_gemini_3_flash_preview(self):
        """
        GEMINI_3_0_FLASH_WITH_FALLBACK should extract 'gemini-3-flash-preview',
        NOT 'gemini-3.0-flash'.
        """
        # Create a mock router config that matches the real GEMINI_3_0_FLASH_WITH_FALLBACK
        router_config = MockLLMRouterConfig(
            model_name="gemini-3.0-flash-gpt-5-mini-fallback-router",
            model_list=[
                MockLLMRouterModelConfig(
                    model_name="vertex-gemini-3.0-flash",
                    litellm_params={"model": "vertex_ai/gemini-3-flash-preview"},
                ),
                MockLLMRouterModelConfig(
                    model_name="gpt-5-mini-fallback",
                    litellm_params={"model": "gpt-5-mini-2025-08-07"},
                ),
            ],
            main_model_group="vertex-gemini-3.0-flash",
        )

        model_name = self._extract_model_name(router_config, "GEMINI_3_0_FLASH_WITH_FALLBACK")

        # Should extract the correct model name with -preview suffix
        assert model_name == "gemini-3-flash-preview", (
            f"Expected 'gemini-3-flash-preview' but got '{model_name}'. "
            "The router config should extract from model_list, not infer from llm_key."
        )

    def test_direct_vertex_config_extracts_correctly(self):
        """Direct VERTEX_GEMINI_3.0_FLASH should extract correctly."""
        direct_config = MockLLMConfig(
            model_name="vertex_ai/gemini-3-flash-preview",
        )

        model_name = self._extract_model_name(direct_config, "VERTEX_GEMINI_3.0_FLASH")
        assert model_name == "gemini-3-flash-preview"

    def test_router_config_extracts_gemini_2_5_flash(self):
        """GEMINI_2_5_FLASH_WITH_FALLBACK should extract 'gemini-2.5-flash'."""
        router_config = MockLLMRouterConfig(
            model_name="gemini-2.5-flash-gpt-5-mini-fallback-router",
            model_list=[
                MockLLMRouterModelConfig(
                    model_name="vertex-gemini-2.5-flash",
                    litellm_params={"model": "vertex_ai/gemini-2.5-flash"},
                ),
                MockLLMRouterModelConfig(
                    model_name="gpt-5-mini-fallback",
                    litellm_params={"model": "gpt-5-mini-2025-08-07"},
                ),
            ],
            main_model_group="vertex-gemini-2.5-flash",
        )

        model_name = self._extract_model_name(router_config, "GEMINI_2_5_FLASH_WITH_FALLBACK")
        assert model_name == "gemini-2.5-flash"

    def test_fallback_to_llm_key_inference_when_no_model_list(self):
        """When there's no model_list, should fall back to llm_key inference."""
        # A config that doesn't have model_list (not a router config)
        simple_config = MockLLMConfig(
            model_name="some-unrelated-name",
        )

        model_name = self._extract_model_name(simple_config, "GEMINI_2_5_FLASH")
        # Should fall back to inference from llm_key
        assert model_name == "gemini-2.5-flash"
