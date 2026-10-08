"""Bridge Skyvern LLM config to OpenAI Agents SDK model + RunConfig.

Known limitations:

* ``resolve_model_config`` takes only ``llm_api_handler`` and has no
  ``prompt_name`` input, so prompt-specific thinking-budget tuning applied by
  ``api_handler_factory`` for certain prompt / model combinations cannot be
  reproduced here.
* ``LLMRouterConfig`` (fallback chains) is accepted by resolving the
  ``main_model_group`` entry as the primary ``LLMConfig`` and passing the
  remaining groups, each with its own credentials, through LiteLLM's
  ``fallbacks`` argument.
  Load-balancing across multiple deployments for the same router group and
  Redis-coordinated cooldowns are not applied on the copilot-v2 path. Proper
  router support through the Agents SDK model interface is tracked in SKY-9256.
"""

from __future__ import annotations

import re
from itertools import count
from typing import Any

import structlog
from agents.extensions.models.litellm_provider import LitellmProvider
from agents.model_settings import ModelSettings
from agents.models.interface import Model
from agents.run_config import RunConfig

from skyvern.config import settings
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMConfigError
from skyvern.forge.sdk.api.llm.litellm_transport import configure_litellm_transport
from skyvern.forge.sdk.copilot.cache_envelope import ROUTER_HOP_FIELDS
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.session_factory import (
    copilot_call_model_input_filter,
    copilot_session_input_callback,
    make_copilot_call_model_input_filter,
)
from skyvern.forge.sdk.copilot.tracing_setup import is_tracing_enabled
from skyvern.schemas.llm import LLMConfig, LLMRouterConfig

LOG = structlog.get_logger()

# Shape of a Skyvern registry alias (e.g. AZURE_OPENAI_GPT5_6_SOL, VERTEX_GEMINI_2.5_PRO, or a
# single-token key like OLLAMA, or a hyphenated one like OPENAI_GPT-4O-2024-08-06): all-caps
# alphanumeric segments, optionally joined by "-"/"_"/".", no provider prefix. Real litellm
# model strings (gpt-4o, azure/gpt-4.1) never match — they are lowercase and/or provider-
# prefixed. Used to fail fast when the copilot is pointed at an alias whose config isn't
# registered here, rather than letting get_config synthesize a provider-less model that 400s
# inside the Agents SDK with "LLM Provider NOT provided". Scoped to this copilot path so the
# self-hosted LLM_KEY synth fallback in get_config stays intact. SKY-12322.
_REGISTRY_STYLE_ALIAS = re.compile(r"[A-Z0-9]+(?:[-_.][A-Z0-9]+)*")

# Keys in litellm_params that are routed elsewhere (top-level kwargs to
# LitellmModel or the dedicated ModelSettings.extra_headers slot), so they
# don't count as "unrouted" when we log dropped keys.
_TOP_LEVEL_ROUTED_FIELDS = frozenset({"api_base", "api_key", "extra_headers", "extra_body"})

# LiteLLMParams fields that LiteLLM consumes as call-level kwargs (splatted
# via ``extra_args`` by the Agents SDK into ``litellm.acompletion(**kwargs)``).
# These ride here so LiteLLM's per-provider translation runs; ``extra_body``
# skips that step and lands the raw, untranslated key in the request body.
_EXTRA_ARGS_FIELDS = frozenset(
    {
        "api_version",
        "model_info",
        "vertex_credentials",
        "vertex_location",
        "timeout",
        "thinking",
        "service_tier",
        "fallbacks",
    }
)

# Dropped at the resolver because the installed LiteLLM has no per-provider
# translation for them; ``extra_args`` would silently no-op and ``extra_body``
# would land the raw, untranslated key in the request body.
_DROP_FIELDS = frozenset({"thinking_level"})

# Track which dropped keys we've already warned about, per process. Avoids
# logging the same warning on every chat-post turn.
_WARNED_DROP_KEYS: set[str] = set()


def _fallback_groups(config: LLMRouterConfig) -> list[str]:
    if not config.fallback_model_group:
        return []
    if isinstance(config.fallback_model_group, str):
        return [config.fallback_model_group]
    return list(config.fallback_model_group)


def _router_fallback_models(config: LLMRouterConfig) -> list[dict[str, Any]]:
    fallbacks: list[dict[str, Any]] = []
    for group in _fallback_groups(config):
        entry = next((m for m in config.model_list if m.model_name == group), None)
        params = entry.litellm_params if entry is not None else {}
        hop = {field: params.get(field) for field in ROUTER_HOP_FIELDS}
        # LiteLLM's Responses route calls .get on model_info, so None crashes the hop.
        hop["model_info"] = params.get("model_info") or {}
        hop["model"] = str(params.get("model") or group)
        hop["base_url"] = params.get("api_base")
        hop["timeout"] = params.get("timeout", settings.LLM_CONFIG_TIMEOUT)
        fallbacks.append(hop)
    return fallbacks


def router_fallback_llm_key(llm_key: str) -> str | None:
    """The registered key a router falls back to, when its fallback group is named after one."""
    if not LLMConfigRegistry.is_registered(llm_key):
        return None
    config = LLMConfigRegistry.get_config(llm_key)
    groups = _fallback_groups(config) if isinstance(config, LLMRouterConfig) else []
    return groups[0] if groups and LLMConfigRegistry.is_registered(groups[0]) else None


def _degrade_router_to_direct(llm_key: str, config: LLMRouterConfig) -> LLMConfig:
    """Resolve an LLMRouterConfig to its primary direct model plus LiteLLM fallbacks.

    The Agents SDK model interface takes a single model, not a router; until the
    full bridge lands (SKY-9256), the copilot-v2 path needs a way to run on
    orgs whose configured llm_key resolves to a router. We use the entry whose
    ``model_name`` matches ``main_model_group``; if none match we fall back to
    ``model_list[0]`` and warn. Each fallback group becomes a LiteLLM fallback
    entry carrying its own model, credentials and tier, passed to the plain
    ``acompletion`` fallback path via ModelSettings.extra_args.

    The happy-path resolution is the expected code path on every copilot-v2
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
    fallback_models = _router_fallback_models(config)
    if fallback_models:
        params["fallbacks"] = fallback_models

    LOG.info(
        "Resolved LLMRouterConfig to primary model on copilot-v2 path",
        llm_key=llm_key,
        main_model_group=config.main_model_group,
        selected_model_name=direct_model_name,
        fallback_models=[hop["model"] for hop in fallback_models],
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


def resolve_model_config(
    llm_api_handler: Any,
    *,
    copilot_config: CopilotConfig | None = None,
    llm_key_override: str | None = None,
) -> tuple[str, RunConfig, str, bool]:
    """Map Skyvern llm_key to OpenAI Agents SDK model string + RunConfig.

    Returns (model_name, run_config, llm_key, supports_vision).
    """
    configure_litellm_transport()

    llm_key = llm_key_override or getattr(llm_api_handler, "llm_key", None) or settings.LLM_KEY
    # GitHub Copilot's OPENAI_COMPATIBLE handler rewrites .llm_key to the bare model name; restore the registry key.
    if (
        llm_key == settings.OPENAI_COMPATIBLE_MODEL_NAME
        and LLMAPIHandlerFactory.is_github_copilot_endpoint()
        and not LLMConfigRegistry.is_registered(llm_key)
    ):
        llm_key = settings.OPENAI_COMPATIBLE_MODEL_KEY

    config = LLMConfigRegistry.get_config(llm_key)

    # get_config synthesizes a config whose model_name IS the llm_key when the key isn't
    # registered. For a registry-style alias (its ENABLE_* flag/credentials unset here) that
    # synthesized model has no provider prefix and 400s inside the Agents SDK with "LLM Provider
    # NOT provided", so fail fast. A registered config or a raw self-hosted model string has
    # model_name != the alias (or isn't registry-style), so the self-hosted synth path and all
    # normal resolutions are untouched. SKY-12322.
    if (
        not LLMConfigRegistry.is_registered(llm_key)
        and isinstance(config, LLMConfig)
        and config.model_name == llm_key
        and _REGISTRY_STYLE_ALIAS.fullmatch(llm_key)
    ):
        raise InvalidLLMConfigError(
            f"copilot llm_key '{llm_key}' looks like a Skyvern LLM registry alias but is not "
            f"registered in this environment; its ENABLE_* flag or provider credentials are likely unset."
        )

    if isinstance(config, LLMRouterConfig):
        config = _degrade_router_to_direct(llm_key, config)

    extra_args: dict[str, Any] = {}
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    base_url: str | None = None
    api_key: str | None = None

    if config.reasoning_effort:
        extra_args["reasoning_effort"] = config.reasoning_effort
        # LiteLLM's chat parameter check rejects reasoning_effort for GPT-6 even on an explicit Responses route.
        if config.model_name.startswith(("openai/responses/", "azure/responses/")):
            extra_args["allowed_openai_params"] = ["reasoning_effort", "service_tier"]

    if isinstance(config, LLMConfig) and config.litellm_params:
        lp = config.litellm_params
        base_url = lp.get("api_base")
        api_key = lp.get("api_key")

        for key in _DROP_FIELDS:
            if lp.get(key) is not None and key not in _WARNED_DROP_KEYS:
                _WARNED_DROP_KEYS.add(key)
                LOG.warning(
                    "Copilot resolver dropped a litellm_params field with no LiteLLM translation in 1.83.7",
                    llm_key=llm_key,
                    dropped_key=key,
                )

        for key in _EXTRA_ARGS_FIELDS:
            val = lp.get(key)
            if val is not None:
                extra_args[key] = val

        headers = lp.get("extra_headers")
        if headers:
            extra_headers = dict(headers)

        body = lp.get("extra_body")
        if body:
            extra_body = dict(body)

        # Warn if litellm_params has keys we don't explicitly route. Covers both
        # future additions to the LiteLLMParams TypedDict and runtime-only keys
        # (typos, dynamically-injected values). Without this, such keys are
        # silently dropped and the call proceeds with a subset of the intended
        # config.
        known_keys = _EXTRA_ARGS_FIELDS | _TOP_LEVEL_ROUTED_FIELDS | _DROP_FIELDS
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

    # ``include_usage=True`` gates ``stream_options={"include_usage": True}`` on
    # streamed chat-completions; without it the final chunk omits token usage.
    model_settings = ModelSettings(
        temperature=config.temperature,
        max_tokens=config.max_completion_tokens or config.max_tokens,
        include_usage=True,
        extra_args=extra_args or None,
        extra_headers=extra_headers,
        extra_body=extra_body,
    )

    provider = CopilotLitellmProvider(base_url=base_url, api_key=api_key)

    run_config = RunConfig(
        model_provider=provider,
        model_settings=model_settings,
        tracing_disabled=not is_tracing_enabled(),
        session_input_callback=copilot_session_input_callback,
        call_model_input_filter=(
            make_copilot_call_model_input_filter(copilot_config.token_budget)
            if copilot_config is not None
            else copilot_call_model_input_filter
        ),
    )

    return config.model_name, run_config, llm_key, config.supports_vision


class CopilotLitellmProvider(LitellmProvider):
    """LitellmProvider that passes per-run base_url/api_key to LitellmModel."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        super().__init__()
        self._base_url = base_url
        self._api_key = api_key
        self._model_call_indices = count(1)

    def get_model(self, model_name: str | None) -> Model:
        from agents.models.default_models import get_default_model

        from skyvern.forge.sdk.copilot.model_telemetry import CopilotLitellmModel

        return CopilotLitellmModel(
            model=model_name or get_default_model(),
            base_url=self._base_url,
            api_key=self._api_key,
            next_model_call_index=self._model_call_indices.__next__,
        )
