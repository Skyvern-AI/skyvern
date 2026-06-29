from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.custom_llms import CustomLLMConfig, CustomLLMProvider
from skyvern.schemas.llm import LiteLLMParams, LLMConfig

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.agent_db import AgentDB

LOG = structlog.get_logger()

_custom_llm_configs: dict[str, CustomLLMConfig] = {}


def custom_llm_key(custom_llm_id: str) -> str:
    return f"CUSTOM_LLM_{custom_llm_id}"


def custom_llm_model_name(custom_llm_id: str) -> str:
    return f"custom/{custom_llm_id}"


def _strip_provider_prefix(model_name: str, prefixes: tuple[str, ...]) -> str:
    for prefix in prefixes:
        if model_name.startswith(prefix):
            return model_name[len(prefix) :]
    return model_name


def _litellm_model_name(config: CustomLLMConfig) -> str:
    if config.provider is CustomLLMProvider.OPENAI_COMPATIBLE:
        return f"openai/{_strip_provider_prefix(config.model_name, ('openai/',))}"
    if config.provider is CustomLLMProvider.OPENROUTER:
        return f"openrouter/{_strip_provider_prefix(config.model_name, ('openrouter/',))}"
    if config.model_name.startswith(("ollama/", "ollama_chat/")):
        return config.model_name
    return f"ollama_chat/{config.model_name}"


def _build_litellm_params(config: CustomLLMConfig, litellm_model_name: str) -> LiteLLMParams:
    params: LiteLLMParams = {
        "api_key": config.api_key,
        "api_base": config.api_base,
        "api_version": config.api_version,
        "model_info": {"model_name": litellm_model_name},
    }
    return {key: value for key, value in params.items() if value is not None}  # type: ignore[return-value]


def _build_llm_config(config: CustomLLMConfig) -> LLMConfig:
    litellm_model_name = _litellm_model_name(config)
    return LLMConfig(
        litellm_model_name,
        [],
        supports_vision=config.supports_vision,
        add_assistant_prefix=config.add_assistant_prefix,
        max_completion_tokens=config.max_completion_tokens,
        temperature=config.temperature,
        litellm_params=_build_litellm_params(config, litellm_model_name),
        reasoning_effort=config.reasoning_effort,
    )


def register_custom_llm_config(custom_llm_id: str, config: CustomLLMConfig) -> None:
    from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry  # noqa: PLC0415

    llm_key = custom_llm_key(custom_llm_id)
    LLMConfigRegistry.deregister_config(llm_key)
    LLMConfigRegistry.register_config(llm_key, _build_llm_config(config))
    _custom_llm_configs[custom_llm_id] = config
    LOG.info(
        "Registered custom LLM",
        custom_llm_id=custom_llm_id,
        provider=config.provider.value,
        model_name=config.model_name,
    )


def deregister_custom_llm_config(custom_llm_id: str) -> None:
    from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry  # noqa: PLC0415

    LLMConfigRegistry.deregister_config(custom_llm_key(custom_llm_id))
    _custom_llm_configs.pop(custom_llm_id, None)


def get_custom_llm_model_mappings() -> dict[str, dict[str, str]]:
    return {
        custom_llm_model_name(custom_llm_id): {
            "llm_key": custom_llm_key(custom_llm_id),
            "label": f"{config.display_name} (Custom)",
        }
        for custom_llm_id, config in _custom_llm_configs.items()
    }


async def load_custom_llm_configs_from_database(database: AgentDB) -> None:
    tokens = await database.organizations.get_valid_org_auth_tokens_by_type(OrganizationAuthTokenType.custom_llm)
    active_ids: set[str] = set()
    for token in tokens:
        try:
            config = CustomLLMConfig.model_validate(json.loads(token.token))
        except Exception:
            LOG.warning("Skipping invalid custom LLM config", custom_llm_id=token.id, exc_info=True)
            continue

        active_ids.add(token.id)
        register_custom_llm_config(token.id, config)

    for custom_llm_id in set(_custom_llm_configs) - active_ids:
        deregister_custom_llm_config(custom_llm_id)
