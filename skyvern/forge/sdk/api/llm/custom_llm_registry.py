from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.custom_llms import CustomLLMConfig, CustomLLMProvider
from skyvern.schemas.llm import LiteLLMParams, LLMConfig

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.agent_db import AgentDB
    from skyvern.forge.sdk.schemas.organizations import Organization

LOG = structlog.get_logger()

CUSTOM_LLM_MODEL_PREFIX = "custom/"


class CustomLLMNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class CustomLLMRegistryEntry:
    organization_id: str
    config: CustomLLMConfig


_custom_llm_configs: dict[str, CustomLLMRegistryEntry] = {}


CUSTOM_LLM_KEY_PREFIX = "CUSTOM_LLM_"


def custom_llm_key(custom_llm_id: str) -> str:
    return f"{CUSTOM_LLM_KEY_PREFIX}{custom_llm_id}"


def is_custom_llm_key(llm_key: str) -> bool:
    return llm_key.startswith(CUSTOM_LLM_KEY_PREFIX)


def custom_llm_model_name(custom_llm_id: str) -> str:
    return f"{CUSTOM_LLM_MODEL_PREFIX}{custom_llm_id}"


def is_custom_llm_model_name(model_name: str) -> bool:
    return model_name.startswith(CUSTOM_LLM_MODEL_PREFIX)


def custom_llm_id_from_model_name(model_name: str) -> str | None:
    if not is_custom_llm_model_name(model_name):
        return None
    custom_llm_id = model_name.removeprefix(CUSTOM_LLM_MODEL_PREFIX)
    return custom_llm_id or None


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
    if config.provider is CustomLLMProvider.GEMINI:
        return f"gemini/{_strip_provider_prefix(config.model_name, ('gemini/',))}"
    if config.model_name.startswith(("ollama/", "ollama_chat/")):
        return config.model_name
    return f"ollama_chat/{config.model_name}"


# Keys _build_litellm_params derives from the config; everything else in litellm_params is
# customer-supplied passthrough. Kept here so dispatch paths can recover the passthrough set.
_CONNECTION_PARAM_KEYS = frozenset({"api_key", "api_base", "api_version", "model_info"})


def _build_litellm_params(config: CustomLLMConfig, litellm_model_name: str) -> LiteLLMParams:
    params: dict[str, Any] = {
        "api_key": config.api_key,
        "api_base": config.api_base,
        "api_version": config.api_version,
        "model_info": {"model_name": litellm_model_name},
    }
    merged = {key: value for key, value in params.items() if value is not None}
    # Provider-specific passthrough (e.g. service_tier, thinking, extra_headers). Applied last so it
    # rides through the same litellm_params merge that reaches the completion call; reserved keys are
    # rejected at config validation, so nothing here can clobber api_key/api_base/model.
    merged.update(config.extra_parameters)
    return merged  # type: ignore[return-value]


def custom_llm_passthrough_parameters(litellm_params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the customer-supplied passthrough params from a registered config's litellm_params.

    Dispatch paths that rebuild the request kwargs from a fixed allowlist (e.g. the custom
    OpenRouter branch) use this to forward provider-specific params they would otherwise drop.
    """
    if not litellm_params:
        return {}
    return {key: value for key, value in litellm_params.items() if key not in _CONNECTION_PARAM_KEYS}


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


def register_custom_llm_config(custom_llm_id: str, organization_id: str, config: CustomLLMConfig) -> None:
    from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry  # noqa: PLC0415

    llm_key = custom_llm_key(custom_llm_id)
    LLMConfigRegistry.deregister_config(llm_key)
    LLMConfigRegistry.register_config(llm_key, _build_llm_config(config))
    _custom_llm_configs[custom_llm_id] = CustomLLMRegistryEntry(organization_id=organization_id, config=config)
    LOG.info(
        "Registered custom LLM",
        custom_llm_id=custom_llm_id,
        organization_id=organization_id,
        provider=config.provider.value,
    )


def deregister_custom_llm_config(custom_llm_id: str) -> None:
    from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry  # noqa: PLC0415

    LLMConfigRegistry.deregister_config(custom_llm_key(custom_llm_id))
    _custom_llm_configs.pop(custom_llm_id, None)


def is_custom_llm_owned_by_organization(custom_llm_id: str, organization_id: str) -> bool:
    entry = _custom_llm_configs.get(custom_llm_id)
    return entry is not None and entry.organization_id == organization_id


def get_custom_llm_model_mappings(organization_id: str | None = None) -> dict[str, dict[str, str]]:
    if organization_id is None:
        return {}

    entries = {
        custom_llm_id: entry
        for custom_llm_id, entry in _custom_llm_configs.items()
        if entry.organization_id == organization_id
    }
    return {
        custom_llm_model_name(custom_llm_id): {
            "llm_key": custom_llm_key(custom_llm_id),
            "label": f"{entry.config.display_name} (Custom {custom_llm_id})",
        }
        for custom_llm_id, entry in entries.items()
    }


async def load_custom_llm_configs_for_organization(database: AgentDB, organization_id: str) -> None:
    tokens = await database.organizations.get_valid_org_auth_tokens(
        organization_id=organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
    )
    active_ids = {token.id for token in tokens}
    for custom_llm_id, entry in list(_custom_llm_configs.items()):
        if entry.organization_id == organization_id and custom_llm_id not in active_ids:
            deregister_custom_llm_config(custom_llm_id)

    for token in tokens:
        try:
            config = CustomLLMConfig.model_validate_json(token.token)
        except Exception as exc:
            LOG.warning(
                "Skipping invalid custom LLM config",
                custom_llm_id=token.id,
                error_type=type(exc).__name__,
            )
            continue
        register_custom_llm_config(token.id, token.organization_id, config)


async def prepare_org_llm_runtime(
    database: AgentDB,
    organization_id: str,
    organization: Organization | None = None,
) -> None:
    if organization is None:
        organization, _ = await asyncio.gather(
            database.organizations.get_organization(organization_id),
            load_custom_llm_configs_for_organization(database, organization_id),
        )
    else:
        await load_custom_llm_configs_for_organization(database, organization_id)

    context = skyvern_context.current()
    if context is None:
        context = SkyvernContext(organization_id=organization_id)
        skyvern_context.set(context)
    context.org_default_llm_key = organization.default_llm_key if organization is not None else None
    context.org_default_secondary_llm_key = organization.default_secondary_llm_key if organization is not None else None


async def ensure_custom_llm_registered_for_org(
    custom_llm_id: str,
    organization_id: str,
    database: AgentDB,
) -> bool:
    if is_custom_llm_owned_by_organization(custom_llm_id, organization_id):
        return True

    tokens = await database.organizations.get_valid_org_auth_tokens(
        organization_id=organization_id,
        token_type=OrganizationAuthTokenType.custom_llm,
    )
    for token in tokens:
        if token.id != custom_llm_id:
            continue
        try:
            config = CustomLLMConfig.model_validate_json(token.token)
        except Exception as exc:
            LOG.warning(
                "Skipping invalid custom LLM config",
                custom_llm_id=token.id,
                error_type=type(exc).__name__,
            )
            return False
        register_custom_llm_config(token.id, token.organization_id, config)
        return True
    return False


async def ensure_custom_llm_model_registered_for_org(
    model_name: str | None,
    organization_id: str,
    database: AgentDB,
) -> None:
    if not model_name or not is_custom_llm_model_name(model_name):
        return

    custom_llm_id = custom_llm_id_from_model_name(model_name)
    if not custom_llm_id:
        raise CustomLLMNotFoundError("Custom LLM model not found for organization")

    registered = await ensure_custom_llm_registered_for_org(
        custom_llm_id,
        organization_id,
        database,
    )
    if not registered:
        raise CustomLLMNotFoundError("Custom LLM model not found for organization")


async def load_custom_llm_configs_from_database(database: AgentDB) -> None:
    tokens = await database.organizations.get_valid_org_auth_tokens_by_type(OrganizationAuthTokenType.custom_llm)
    active_ids: set[str] = set()
    for token in tokens:
        try:
            config = CustomLLMConfig.model_validate(json.loads(token.token))
        except Exception as exc:
            LOG.warning(
                "Skipping invalid custom LLM config",
                custom_llm_id=token.id,
                error_type=type(exc).__name__,
            )
            continue

        active_ids.add(token.id)
        register_custom_llm_config(token.id, token.organization_id, config)

    for custom_llm_id in set(_custom_llm_configs) - active_ids:
        deregister_custom_llm_config(custom_llm_id)
