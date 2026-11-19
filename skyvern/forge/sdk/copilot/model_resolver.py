"""Copilot's RunConfig: the shared Agents SDK model plus Copilot's token budget, session callbacks and input filter."""

from __future__ import annotations

import re
from typing import Any

import litellm
from agents.run_config import RunConfig

from skyvern.forge.sdk.api.llm.agents_model_resolver import resolve_agents_model
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.router_fallback import fallback_groups
from skyvern.forge.sdk.copilot.config import DEFAULT_TOKEN_BUDGET, CopilotConfig
from skyvern.forge.sdk.copilot.enforcement import stable_prefix_anchors
from skyvern.forge.sdk.copilot.session_factory import (
    copilot_session_input_callback,
    make_copilot_call_model_input_filter,
)
from skyvern.forge.sdk.copilot.tracing_setup import is_tracing_enabled
from skyvern.schemas.llm import LLMConfig, LLMRouterConfig

_INPUT_PRICE_ABOVE_PROMPT_SIZE = re.compile(r"input_cost_per_token_above_(\d+)k_tokens")
# Billed input tokens per token of the budget's estimate (conversation items, OpenAI's tokenizer), and the system prompt
# and tool list it leaves out. Production calls billed up to 26k past it on Terra and Sol and up to 60k past 1.35x it on
# Sonnet 5.5; captured inputs billed up to 1.37x on Sonnet 5.5.
_OPENAI_COUNTS = (1.0, 30_000)
_CLAUDE_COUNTS = (1.4, 65_000)
# Sonnet 5.5's 1M window less its 128k max output. LiteLLM's table leaves the window out, and adding it there would
# also raise Task V3's prompt limit, which counts OpenAI tokens.
_CLAUDE_INPUT_LIMITS = {"claude-sonnet-5-5": 872_000}


def _model_token_budget(model: str, model_info: dict[str, Any]) -> int:
    # LiteLLM files OpenAI and Anthropic prices without the provider prefix.
    for name in (model_info.get("model_name"), model, model.split("/", 1)[-1]):
        if not isinstance(name, str) or not (prices := litellm.model_cost.get(name)):
            continue
        # register_model fills every price key LiteLLM knows, so a None price is one the model does not charge.
        limits = [
            int(match.group(1)) * 1000
            for key, price in prices.items()
            if price is not None and (match := _INPUT_PRICE_ABOVE_PROMPT_SIZE.fullmatch(key))
        ]
        if prices.get("max_input_tokens"):
            limits.append(prices["max_input_tokens"] - (prices.get("max_output_tokens") or 0))
        if prices.get("litellm_provider") in ("openai", "azure"):
            ratio, unestimated = _OPENAI_COUNTS
        elif "claude" in name:
            ratio, unestimated = _CLAUDE_COUNTS
            # bedrock/global.anthropic.claude-sonnet-5-5 -> claude-sonnet-5-5
            if limit := _CLAUDE_INPUT_LIMITS.get(name.rsplit("/", 1)[-1].rsplit(".", 1)[-1]):
                limits.append(limit)
        else:
            return DEFAULT_TOKEN_BUDGET
        if not limits:
            return DEFAULT_TOKEN_BUDGET
        return max(DEFAULT_TOKEN_BUDGET, int((min(limits) - unestimated) / ratio))
    return DEFAULT_TOKEN_BUDGET


def copilot_token_budget(copilot_config: CopilotConfig | None, llm_key: str) -> int:
    return _token_budget(copilot_config, LLMConfigRegistry.get_config(llm_key))


def _token_budget(copilot_config: CopilotConfig | None, config: LLMConfig | LLMRouterConfig) -> int:
    """The configured budget, else the largest that keeps every model in config under its short-context price and
    inside its window, never below DEFAULT_TOKEN_BUDGET. A model with neither limit known, or with a tokenizer the
    budget has no conversion for, holds it to DEFAULT_TOKEN_BUDGET."""
    if copilot_config is not None and copilot_config.token_budget is not None:
        return copilot_config.token_budget
    if isinstance(config, LLMRouterConfig):
        groups = {config.main_model_group, *fallback_groups(config)}
        models = [
            (
                str(entry.litellm_params.get("model") or entry.model_name),
                entry.model_info or entry.litellm_params.get("model_info") or {},
            )
            for entry in config.model_list
            if entry.model_name in groups
        ]
    else:
        models = [(config.model_name, (config.litellm_params or {}).get("model_info") or {})]
    return min((_model_token_budget(model, info) for model, info in models), default=DEFAULT_TOKEN_BUDGET)


def resolve_model_config(
    llm_api_handler: Any,
    *,
    copilot_config: CopilotConfig | None = None,
    llm_key_override: str | None = None,
) -> tuple[str, RunConfig, str, bool]:
    """Map Skyvern llm_key to OpenAI Agents SDK model string + RunConfig.

    Returns (model_name, run_config, llm_key, supports_vision).
    """
    resolved = resolve_agents_model(
        llm_key_override or getattr(llm_api_handler, "llm_key", None),
        agent_name="ember",
        prefix_anchors=stable_prefix_anchors,
    )
    run_config = RunConfig(
        model=resolved.model,
        model_provider=resolved.provider,
        model_settings=resolved.model_settings,
        tracing_disabled=not is_tracing_enabled(),
        session_input_callback=copilot_session_input_callback,
        call_model_input_filter=make_copilot_call_model_input_filter(
            copilot_token_budget(copilot_config, resolved.llm_key)
        ),
    )
    return resolved.model_name, run_config, resolved.llm_key, resolved.supports_vision
