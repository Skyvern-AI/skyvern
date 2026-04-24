"""Bridge Skyvern LLM config to OpenAI Agents SDK model + RunConfig.

Known limitations:

* ``resolve_model_config`` takes only ``llm_api_handler`` and has no
  ``prompt_name`` input, so prompt-specific thinking-budget tuning applied by
  ``api_handler_factory`` for certain prompt / model combinations cannot be
  reproduced here.
* ``LLMRouterConfig`` (fallback chains) is accepted by degrading to the
  ``main_model_group`` entry as a direct ``LLMConfig``. Load-balancing across
  ``model_list``, cross-provider fallbacks, and Redis-coordinated cooldowns
  are not applied on the copilot-v2 path. Proper router support through the
  Agents SDK model interface is tracked in SKY-9256.
"""

from __future__ import annotations

from typing import Any

import structlog
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

LOG = structlog.get_logger()

# Keys in litellm_params that are routed elsewhere (top-level kwargs to
# LitellmModel or the dedicated ModelSettings.extra_headers slot), so they
# don't count as "unrouted" when we log dropped keys.
_TOP_LEVEL_ROUTED_FIELDS = frozenset({"api_base", "api_key", "extra_headers"})

# LiteLLMParams fields that LiteLLM consumes as call-level kwargs (splatted
# via ``extra_args`` by the Agents SDK into ``litellm.acompletion(**kwargs)``).
_EXTRA_ARGS_FIELDS = frozenset(
    {
        "api_version",
        "model_info",
        "vertex_credentials",
        "vertex_location",
        "timeout",
    }
)

# LiteLLMParams fields that end up as provider-specific payload-body entries
# (routed via ``ModelSettings.extra_body`` → request JSON body).
_EXTRA_BODY_FIELDS = frozenset({"thinking", "thinking_level", "service_tier"})


def _degrade_router_to_direct(llm_key: str, config: LLMRouterConfig) -> LLMConfig:
    """Collapse an LLMRouterConfig down to its main_model_group entry as a direct LLMConfig.

    The Agents SDK model interface takes a single model, not a router; until the
    full bridge lands (SKY-9256), the copilot-v2 path needs a way to run on
    orgs whose configured llm_key resolves to a router. We use the entry whose
    ``model_name`` matches ``main_model_group``; if none match we fall back to
    ``model_list[0]`` and warn.

    The happy-path degradation is the expected code path on every copilot-v2
    call in staging/prod, so it logs at INFO. WARN is reserved for the
    main_model_group-miss misconfig case.
    """
    if not config.model_list:
        raise InvalidLLMConfigError(
            f"llm_key '{llm_key}' is an LLMRouterConfig with an empty model_list; cannot resolve a model."
        )

    selected = next((m for m in config.model_list if m.model_name == config.main_model_group), None)
    if selected is None:
        LOG.warning(
            "LLMRouterConfig main_model_group has no matching model_list entry; using model_list[0]",
            llm_key=llm_key,
            main_model_group=config.main_model_group,
            available_groups=sorted({m.model_name for m in config.model_list}),
        )
        selected = config.model_list[0]

    # LLMRouterModelConfig.litellm_params carries the real litellm model string
    # in its "model" key (e.g. "vertex_ai/gemini-2.5-flash"); the outer
    # entry.model_name is just a router group alias.
    params = dict(selected.litellm_params)
    direct_model_name = params.pop("model", None) or selected.model_name

    LOG.info(
        "Degrading LLMRouterConfig to main model on copilot-v2 path; fallbacks/load-balancing not applied",
        llm_key=llm_key,
        main_model_group=config.main_model_group,
        selected_model_name=direct_model_name,
    )

    return LLMConfig(
        model_name=direct_model_name,
        required_env_vars=list(config.required_env_vars),
        supports_vision=config.supports_vision,
        add_assistant_prefix=config.add_assistant_prefix,
        litellm_params=params or None,  # type: ignore[arg-type]
        max_tokens=config.max_tokens,
        max_completion_tokens=config.max_completion_tokens,
        temperature=config.temperature,
        reasoning_effort=config.reasoning_effort,
    )


def resolve_model_config(llm_api_handler: Any) -> tuple[str, RunConfig, str, bool]:
    """Map Skyvern llm_key to OpenAI Agents SDK model string + RunConfig.

    Returns (model_name, run_config, llm_key, supports_vision).
    """
    llm_key = getattr(llm_api_handler, "llm_key", None) or settings.LLM_KEY
    config = LLMConfigRegistry.get_config(llm_key)

    if isinstance(config, LLMRouterConfig):
        config = _degrade_router_to_direct(llm_key, config)

    extra_body: dict[str, Any] = {}
    extra_args: dict[str, Any] = {}
    extra_headers: dict[str, str] | None = None
    base_url: str | None = None
    api_key: str | None = None

    if config.reasoning_effort:
        extra_body["reasoning_effort"] = config.reasoning_effort

    if isinstance(config, LLMConfig) and config.litellm_params:
        lp = config.litellm_params
        base_url = lp.get("api_base")
        api_key = lp.get("api_key")

        for key in _EXTRA_ARGS_FIELDS:
            val = lp.get(key)
            if val is not None:
                extra_args[key] = val

        for key in _EXTRA_BODY_FIELDS:
            val = lp.get(key)
            if val is not None:
                extra_body[key] = val

        headers = lp.get("extra_headers")
        if headers:
            extra_headers = dict(headers)

        # Warn if litellm_params has keys we don't explicitly route. Covers both
        # future additions to the LiteLLMParams TypedDict and runtime-only keys
        # (typos, dynamically-injected values). Without this, such keys are
        # silently dropped and the call proceeds with a subset of the intended
        # config.
        known_keys = _EXTRA_ARGS_FIELDS | _EXTRA_BODY_FIELDS | _TOP_LEVEL_ROUTED_FIELDS
        unrouted = sorted(k for k in lp.keys() if k not in known_keys)
        if unrouted:
            LOG.warning(
                "litellm_params contains keys not routed by resolve_model_config; they will be dropped",
                llm_key=llm_key,
                unrouted_keys=unrouted,
            )

    # Default timeout parity with the non-SDK handler (api_handler_factory:
    # injects settings.LLM_CONFIG_TIMEOUT when litellm_params has no timeout).
    if "timeout" not in extra_args:
        extra_args["timeout"] = settings.LLM_CONFIG_TIMEOUT

    model_settings = ModelSettings(
        temperature=config.temperature,
        max_tokens=config.max_completion_tokens or config.max_tokens,
        extra_body=extra_body or None,
        extra_args=extra_args or None,
        extra_headers=extra_headers,
    )

    provider = CopilotLitellmProvider(base_url=base_url, api_key=api_key)

    run_config = RunConfig(
        model_provider=provider,
        model_settings=model_settings,
        tracing_disabled=not is_tracing_enabled(),
        session_input_callback=copilot_session_input_callback,
        call_model_input_filter=copilot_call_model_input_filter,
    )

    return config.model_name, run_config, llm_key, config.supports_vision


class CopilotLitellmProvider(LitellmProvider):
    """LitellmProvider that passes per-run base_url/api_key to LitellmModel."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        super().__init__()
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
