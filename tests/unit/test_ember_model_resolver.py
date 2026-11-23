"""Tests for resolve_model_config: bridges Skyvern LLM config to OpenAI Agents SDK."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from agents import Agent, Runner
from litellm.types.utils import ModelResponse

from skyvern.forge.sdk.api.llm.agents_model import SkyvernLitellmModel
from skyvern.forge.sdk.ember.enforcement import stable_prefix_anchors
from skyvern.forge.sdk.ember.model_resolver import resolve_model_config
from skyvern.schemas.llm import LLMConfig


def _install_config(monkeypatch: pytest.MonkeyPatch, config: Any, llm_key: str) -> MagicMock:
    if config is not None:
        monkeypatch.setattr(
            "skyvern.forge.sdk.ember.model_resolver.LLMConfigRegistry.get_config",
            lambda key: config,
        )
    handler = MagicMock()
    handler.llm_key = llm_key
    return handler


class TestModelResolver:
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
            "skyvern.forge.sdk.ember.model_resolver.LLMConfigRegistry.get_config",
            fake_get_config,
        )

        handler = _install_config(monkeypatch, None, "PRIMARY_KEY")

        model_name, _, llm_key, _ = resolve_model_config(handler, llm_key_override="FALLBACK_KEY")

        assert model_name == "openai/fallback_key"
        assert llm_key == "FALLBACK_KEY"
        assert set(seen_keys) == {"FALLBACK_KEY"}

    def test_ember_model_is_traced_labelled_ember_and_anchors_on_ember_rewrites(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EMBER_TRACING_ENABLED", "1")
        config = LLMConfig(
            model_name="openai/gpt-5.6",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
        )
        _, run_config, _, _ = resolve_model_config(_install_config(monkeypatch, config, "EMBER_KEY"))

        assert run_config.tracing_disabled is False
        assert isinstance(run_config.model, SkyvernLitellmModel)
        assert run_config.model.agent_name == "ember"
        assert run_config.model.prefix_anchors is stable_prefix_anchors
        assert run_config.session_input_callback is not None
        assert run_config.call_model_input_filter is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", ["gpt-5.4", "gpt-5", "azure/gpt-5.6-terra"])
async def test_a_ember_turn_sends_the_configured_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch, model_name: str
) -> None:
    sent: list[dict[str, object]] = []

    async def fake_acompletion(**kwargs: object) -> ModelResponse:
        sent.append(kwargs)
        return ModelResponse(choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}}])

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    config = LLMConfig(
        model_name=model_name,
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        reasoning_effort="high",
    )
    resolved_model_name, run_config, _, _ = resolve_model_config(_install_config(monkeypatch, config, "GPT5_TEST"))

    await Runner.run(Agent(name="workflow-copilot", model=resolved_model_name), "hello", run_config=run_config)

    (request,) = sent
    assert request["reasoning_effort"] == "high"
    assert "verbosity" not in request
