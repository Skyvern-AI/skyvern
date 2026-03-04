"""Bridge Skyvern LLM config to OpenAI Agents SDK model + RunConfig."""

from __future__ import annotations

from typing import Any

from agents.extensions.models.litellm_provider import LitellmProvider
from agents.model_settings import ModelSettings
from agents.models.interface import Model
from agents.run_config import RunConfig

from skyvern.config import settings
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMConfigError
from skyvern.forge.sdk.api.llm.models import LLMConfig, LLMRouterConfig
from skyvern.forge.sdk.copilot.session_factory import (
    copilot_call_model_input_filter,
    copilot_session_input_callback,
)
from skyvern.forge.sdk.copilot.tracing_setup import is_tracing_enabled


def resolve_model_config(llm_api_handler: Any) -> tuple[str, RunConfig, str, bool]:
    """Map Skyvern llm_key to OpenAI Agents SDK model string + RunConfig.

    Returns (model_name, run_config, llm_key, supports_vision).
    """
    llm_key = getattr(llm_api_handler, "llm_key", None) or settings.LLM_KEY
    config = LLMConfigRegistry.get_config(llm_key)

    if isinstance(config, LLMRouterConfig):
        raise InvalidLLMConfigError(
            f"llm_key '{llm_key}' uses LLMRouterConfig which is not yet supported. "
            "Use a non-router LLMConfig llm_key instead."
        )

    model_name = config.model_name

    model_settings = ModelSettings(
        temperature=config.temperature,
        max_tokens=config.max_completion_tokens or config.max_tokens,
    )

    extra_body: dict[str, Any] = {}
    litellm_extra: dict[str, Any] = {}

    if config.reasoning_effort:
        extra_body["reasoning_effort"] = config.reasoning_effort

    if isinstance(config, LLMConfig) and config.litellm_params:
        lp = config.litellm_params
        if lp.get("api_base"):
            litellm_extra["api_base"] = lp["api_base"]
        if lp.get("api_key"):
            litellm_extra["api_key"] = lp["api_key"]
        if lp.get("api_version"):
            litellm_extra["api_version"] = lp["api_version"]
        if lp.get("thinking"):
            extra_body["thinking"] = lp["thinking"]

    if extra_body:
        model_settings.extra_body = extra_body

    if litellm_extra.get("api_version"):
        model_settings.extra_args = model_settings.extra_args or {}
        model_settings.extra_args["api_version"] = litellm_extra["api_version"]

    provider = CopilotLitellmProvider(
        base_url=litellm_extra.get("api_base"),
        api_key=litellm_extra.get("api_key"),
    )

    run_config = RunConfig(
        model_provider=provider,
        model_settings=model_settings,
        tracing_disabled=not is_tracing_enabled(),
        session_input_callback=copilot_session_input_callback,
        call_model_input_filter=copilot_call_model_input_filter,
    )

    return model_name, run_config, llm_key, config.supports_vision


class CopilotLitellmProvider(LitellmProvider):
    """LitellmProvider that passes per-run base_url/api_key to LitellmModel."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._base_url = base_url
        self._api_key = api_key

    def get_model(self, model_name: str | None) -> Model:
        from agents.extensions.models.litellm_model import LitellmModel
        from agents.models.default_models import get_default_model

        return LitellmModel(
            model=model_name or get_default_model(),
            base_url=self._base_url,
            api_key=self._api_key,
        )
