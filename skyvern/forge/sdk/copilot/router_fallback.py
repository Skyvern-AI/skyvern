from __future__ import annotations

from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.schemas.llm import LLMRouterConfig


def fallback_groups(config: LLMRouterConfig) -> list[str]:
    if not config.fallback_model_group:
        return []
    if isinstance(config.fallback_model_group, str):
        return [config.fallback_model_group]
    return list(config.fallback_model_group)


def router_fallback_llm_key(llm_key: str) -> str | None:
    """The registered key a router falls back to, when its fallback group is named after one."""
    if not LLMConfigRegistry.is_registered(llm_key):
        return None
    config = LLMConfigRegistry.get_config(llm_key)
    groups = fallback_groups(config) if isinstance(config, LLMRouterConfig) else []
    return groups[0] if groups and LLMConfigRegistry.is_registered(groups[0]) else None
