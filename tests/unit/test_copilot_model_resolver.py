"""Tests for resolve_model_config: bridges Skyvern LLM config to OpenAI Agents SDK."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs


class TestModelResolver:
    def test_router_config_empty_model_list_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMConfigError
        from skyvern.schemas.llm import LLMRouterConfig

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

        with pytest.raises(InvalidLLMConfigError, match="empty model_list"):
            resolve_model_config(handler)

    def test_router_config_resolves_primary_model_with_litellm_fallbacks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Router keys resolve to a primary model while preserving LiteLLM fallbacks.

        This is still not full router parity: deployment load-balancing and
        cooldowns remain outside the Agents SDK path until SKY-9256. The
        fallback chain itself must be carried through so Bedrock-primary
        Copilot keys can fall through when the primary provider is unavailable.
        """
        from skyvern.schemas.llm import LLMRouterConfig, LLMRouterModelConfig

        main = LLMRouterModelConfig(
            model_name="vertex-gemini-2.5-flash",  # router group alias
            litellm_params={
                "model": "vertex_ai/gemini-2.5-flash",
                "api_base": "https://vertex.example.com",
                "timeout": 900.0,
            },
        )
        fallback = LLMRouterModelConfig(
            model_name="gpt-4-1-mini-fallback",
            litellm_params={"model": "azure/gpt-4-1-mini"},
        )
        final_fallback = LLMRouterModelConfig(
            model_name="claude-fallback",
            litellm_params={"model": "anthropic/claude-sonnet-4-20250514"},
        )
        router_config = LLMRouterConfig(
            model_name="gemini-2.5-flash-fallback-router",
            model_list=[main, fallback, final_fallback],
            required_env_vars=["VERTEX_CREDENTIALS"],
            supports_vision=True,
            add_assistant_prefix=False,
            main_model_group="vertex-gemini-2.5-flash",
            fallback_model_group=["gpt-4-1-mini-fallback", "claude-fallback"],
            temperature=0.3,
            max_completion_tokens=8192,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: router_config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "GEMINI_2_5_FLASH_WITH_FALLBACK"

        model_name, run_config, llm_key, supports_vision = resolve_model_config(handler)

        assert model_name == "vertex_ai/gemini-2.5-flash"
        assert llm_key == "GEMINI_2_5_FLASH_WITH_FALLBACK"
        assert supports_vision is True
        assert run_config.model_settings is not None
        assert run_config.model_settings.temperature == 0.3
        assert run_config.model_settings.max_tokens == 8192
        assert run_config.model_settings.extra_args is not None
        assert run_config.model_settings.extra_args["timeout"] == 900.0
        assert run_config.model_settings.extra_args["fallbacks"] == [
            "azure/gpt-4-1-mini",
            "anthropic/claude-sonnet-4-20250514",
        ]

    def test_router_config_no_main_group_match_falls_back_to_first_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.schemas.llm import LLMRouterConfig, LLMRouterModelConfig

        entry = LLMRouterModelConfig(
            model_name="some-group",
            litellm_params={"model": "vertex_ai/gemini-2.5-flash"},
        )
        router_config = LLMRouterConfig(
            model_name="misconfigured-router",
            model_list=[entry],
            required_env_vars=[],
            supports_vision=False,
            add_assistant_prefix=False,
            main_model_group="nonexistent-group",
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: router_config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "MISCONFIGURED_ROUTER"

        with capture_logs() as logs:
            model_name, _, _, _ = resolve_model_config(handler)

        assert model_name == "vertex_ai/gemini-2.5-flash"
        joined = " ".join(str(record.get("event", "")) for record in logs)
        assert "main_model_group has no matching" in joined

    def test_maps_basic_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.schemas.llm import LLMConfig

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

    def test_llm_key_override_wins_for_fallback_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.schemas.llm import LLMConfig

        seen_keys: list[str] = []

        def fake_get_config(key: str) -> LLMConfig:
            seen_keys.append(key)
            return LLMConfig(
                model_name=f"openai/{key.lower()}",
                required_env_vars=[],
                supports_vision=True,
                add_assistant_prefix=False,
            )

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            fake_get_config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "PRIMARY_KEY"

        model_name, _, llm_key, _ = resolve_model_config(handler, llm_key_override="FALLBACK_KEY")

        assert model_name == "openai/fallback_key"
        assert llm_key == "FALLBACK_KEY"
        assert seen_keys == ["FALLBACK_KEY"]

    def test_maps_basic_config_with_tracing_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.schemas.llm import LLMConfig

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
        from skyvern.schemas.llm import LLMConfig

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
        from skyvern.forge.sdk.copilot import model_resolver as model_resolver_module
        from skyvern.schemas.llm import LiteLLMParams, LLMConfig

        # Reset the per-process warn-once gate so the caplog assertion is
        # deterministic regardless of test ordering.
        model_resolver_module._WARNED_DROP_KEYS.clear()

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

        with capture_logs() as logs:
            _, run_config, _, _ = resolve_model_config(handler)
        ms = run_config.model_settings
        assert ms is not None

        assert ms.extra_headers == {"X-Skyvern-Route": "copilot"}

        assert ms.extra_args is not None
        assert ms.extra_args["thinking"] == lp["thinking"]
        assert ms.extra_args["service_tier"] == lp["service_tier"]
        if ms.extra_body is not None:
            assert "thinking" not in ms.extra_body
            assert "service_tier" not in ms.extra_body

        assert "thinking_level" not in ms.extra_args
        if ms.extra_body is not None:
            assert "thinking_level" not in ms.extra_body
        assert any(record.get("dropped_key") == "thinking_level" for record in logs)

        for field in ("api_version", "model_info", "vertex_credentials", "vertex_location", "timeout"):
            assert ms.extra_args[field] == lp[field]

    def test_default_timeout_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When litellm_params has no timeout, inject settings.LLM_CONFIG_TIMEOUT."""
        from skyvern.config import settings
        from skyvern.schemas.llm import LLMConfig

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

    def test_disables_litellm_aiohttp_transport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import litellm

        from skyvern.schemas.llm import LLMConfig

        monkeypatch.setattr(litellm, "disable_aiohttp_transport", False)
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
        handler.llm_key = "BASIC_KEY"

        resolve_model_config(handler)

        assert litellm.disable_aiohttp_transport is True

    def test_explicit_timeout_wins_over_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.schemas.llm import LiteLLMParams, LLMConfig

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

    def test_warns_on_unrouted_litellm_params_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keys in litellm_params that aren't explicitly routed should produce
        a LOG.warning listing the dropped keys — covers typos, dynamically
        injected values, and future additions to LiteLLMParams that we
        haven't updated the routing for."""
        from skyvern.schemas.llm import LLMConfig

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
            litellm_params=lp,  # type: ignore[arg-type]
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "WITH_TYPO_KEY"

        with capture_logs() as logs:
            resolve_model_config(handler)

        joined = " ".join(str(record.get("unrouted_keys", "")) for record in logs)
        assert "future_litellm_addition" in joined
        assert "typo_feild_name" in joined

    def test_no_warning_when_all_litellm_params_are_routed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.schemas.llm import LiteLLMParams, LLMConfig

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

        with capture_logs() as logs:
            resolve_model_config(handler)

        joined = " ".join(str(record.get("event", "")) for record in logs)
        assert "unrouted" not in joined

    def test_github_copilot_endpoint_restores_openai_compatible_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GitHub Copilot via OPENAI_COMPATIBLE exposes the handler's .llm_key as the bare
        model name ("gpt-4o"), which is not a registry key. The resolver must restore the
        OPENAI_COMPATIBLE key so the githubcopilot api_base/api_key thread through instead of
        resolving to a credential-less OpenAI model that 401s."""
        from skyvern.config import settings
        from skyvern.schemas.llm import LiteLLMParams, LLMConfig

        openai_compatible_config = LLMConfig(
            model_name="openai/gpt-4o",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            litellm_params=LiteLLMParams(
                api_key="gho_test",
                api_base="https://api.githubcopilot.com",
                api_version=None,
                model_info={"model_name": "openai/gpt-4o"},
            ),
        )

        seen_keys: list[str] = []

        def fake_get_config(key: str) -> LLMConfig:
            seen_keys.append(key)
            return openai_compatible_config

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.get_config",
            fake_get_config,
        )
        # The rewritten label "gpt-4o" is not a registered registry key.
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMConfigRegistry.is_registered",
            lambda key: False,
        )
        # Force the GitHub Copilot endpoint branch on.
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.model_resolver.LLMAPIHandlerFactory.is_github_copilot_endpoint",
            lambda: True,
        )
        monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MODEL_KEY", "OPENAI_COMPATIBLE")
        monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MODEL_NAME", "gpt-4o")

        from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config

        handler = MagicMock()
        handler.llm_key = "gpt-4o"  # the rewritten observability label

        model_name, run_config, llm_key, _ = resolve_model_config(handler)

        assert llm_key == "OPENAI_COMPATIBLE"
        assert seen_keys == ["OPENAI_COMPATIBLE"]
        assert model_name == "openai/gpt-4o"
        assert run_config.model_provider._base_url == "https://api.githubcopilot.com"
        assert run_config.model_provider._api_key == "gho_test"
