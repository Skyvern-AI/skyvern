"""Tests for resolve_model_config: bridges Skyvern LLM config to OpenAI Agents SDK."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


class TestModelResolver:
    def test_rejects_router_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMConfigError
        from skyvern.forge.sdk.api.llm.models import LLMRouterConfig

        router_config = LLMRouterConfig(
            model_name="test",
            model_list=[],
            required_env_vars=[],
            supports_vision=False,
            add_assistant_prefix=False,
            main_model_group="default",
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: router_config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "ROUTER_KEY"

        with pytest.raises(InvalidLLMConfigError, match="LLMRouterConfig"):
            resolve_model_config(handler)

    def test_maps_basic_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.models import LLMConfig

        monkeypatch.delenv("COPILOT_TRACING_ENABLED", raising=False)
        config = LLMConfig(
            model_name="anthropic/claude-sonnet-4-20250514",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            temperature=0.5,
            max_tokens=4096,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "BASIC_KEY"

        model_name, run_config, llm_key, supports_vision = resolve_model_config(handler)

        assert model_name == "anthropic/claude-sonnet-4-20250514"
        assert llm_key == "BASIC_KEY"
        assert supports_vision is True
        assert run_config.tracing_disabled is True
        assert run_config.model_settings is not None
        assert run_config.model_settings.temperature == 0.5
        assert run_config.model_settings.max_tokens == 4096

    def test_maps_basic_config_with_tracing_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.models import LLMConfig

        monkeypatch.setenv("COPILOT_TRACING_ENABLED", "1")
        config = LLMConfig(
            model_name="anthropic/claude-sonnet-4-20250514",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            temperature=0.5,
            max_tokens=4096,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "BASIC_KEY"

        _, run_config, _, _ = resolve_model_config(handler)

        assert run_config.tracing_disabled is False

    def test_returns_supports_vision_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.models import LLMConfig

        config = LLMConfig(
            model_name="openai/gpt-4-turbo",
            required_env_vars=[],
            supports_vision=False,
            add_assistant_prefix=False,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "NO_VISION_KEY"

        _, _, _, supports_vision = resolve_model_config(handler)
        assert supports_vision is False

    def test_routes_all_litellm_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All LiteLLMParams fields land in the correct ModelSettings slot.

        Guards against COMP-3: silently dropping Vertex/Azure provider fields
        by routing them into the wrong bucket.
        """
        from skyvern.forge.sdk.api.llm.models import LiteLLMParams, LLMConfig

        lp: LiteLLMParams = {
            "api_base": "https://vertex.example.com",
            "api_key": "sk-test",
            "api_version": "2024-02-01",
            "model_info": {"family": "gemini"},
            "vertex_credentials": "creds-blob",
            "vertex_location": "us-central1",
            "thinking": {"type": "enabled"},
            "thinking_level": "high",
            "service_tier": "flex",
            "extra_headers": {"X-Skyvern-Route": "copilot"},
            "timeout": 900.0,
        }
        config = LLMConfig(
            model_name="vertex_ai/gemini-pro",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            litellm_params=lp,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "VERTEX_KEY"

        _, run_config, _, _ = resolve_model_config(handler)
        ms = run_config.model_settings
        assert ms is not None

        assert ms.extra_headers == {"X-Skyvern-Route": "copilot"}
        for field in ("thinking", "thinking_level", "service_tier"):
            assert ms.extra_body is not None
            assert ms.extra_body[field] == lp[field]
        assert ms.extra_args is not None
        for field in ("api_version", "model_info", "vertex_credentials", "vertex_location", "timeout"):
            assert ms.extra_args[field] == lp[field]

    def test_default_timeout_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When litellm_params has no timeout, inject settings.LLM_CONFIG_TIMEOUT."""
        from skyvern.config import settings
        from skyvern.forge.sdk.api.llm.models import LLMConfig

        config = LLMConfig(
            model_name="openai/gpt-4",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "NO_TIMEOUT_KEY"

        _, run_config, _, _ = resolve_model_config(handler)
        assert run_config.model_settings is not None
        assert run_config.model_settings.extra_args is not None
        assert run_config.model_settings.extra_args["timeout"] == settings.LLM_CONFIG_TIMEOUT

    def test_explicit_timeout_wins_over_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.models import LiteLLMParams, LLMConfig

        lp: LiteLLMParams = {"timeout": 123.0}
        config = LLMConfig(
            model_name="openai/gpt-4",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            litellm_params=lp,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "EXPLICIT_TIMEOUT_KEY"

        _, run_config, _, _ = resolve_model_config(handler)
        assert run_config.model_settings is not None
        assert run_config.model_settings.extra_args is not None
        assert run_config.model_settings.extra_args["timeout"] == 123.0

    def test_warns_on_unrouted_litellm_params_keys(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Keys in litellm_params that aren't explicitly routed should produce
        a LOG.warning listing the dropped keys — covers typos, dynamically
        injected values, and future additions to LiteLLMParams that we
        haven't updated the routing for."""
        import logging

        from skyvern.forge.sdk.api.llm.models import LLMConfig

        # Build a dict that bypasses TypedDict type-checking for the unknown key.
        lp: dict[str, Any] = {
            "api_base": "https://example.com",
            "typo_feild_name": "some-value",
            "future_litellm_addition": {"nested": True},
        }
        config = LLMConfig(
            model_name="openai/gpt-4",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            litellm_params=lp,  # type: ignore[typeddict-item]
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "WITH_TYPO_KEY"

        with caplog.at_level(logging.WARNING, logger="skyvern.forge.sdk.copilot.model_resolver"):
            resolve_model_config(handler)

        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "future_litellm_addition" in joined
        assert "typo_feild_name" in joined

    def test_no_warning_when_all_litellm_params_are_routed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from skyvern.forge.sdk.api.llm.models import LiteLLMParams, LLMConfig

        lp: LiteLLMParams = {"api_base": "https://example.com", "timeout": 60.0}
        config = LLMConfig(
            model_name="openai/gpt-4",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            litellm_params=lp,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "CLEAN_KEY"

        with caplog.at_level(logging.WARNING, logger="skyvern.forge.sdk.copilot.model_resolver"):
            resolve_model_config(handler)

        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "unrouted" not in joined
