from __future__ import annotations

import json

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory

LOG = structlog.get_logger()


async def get_llm_config_by_prompt_type(distinct_id: str, organization_id: str | None = None) -> dict[str, str] | None:
    """Return PostHog-configured LLM mapping for each prompt type."""
    llm_config_experiment = await app.EXPERIMENTATION_PROVIDER.get_value(
        "LLM_CONFIG_BY_PROMPT_TYPE", distinct_id, properties={"organization_id": organization_id}
    )
    if llm_config_experiment in (False, "False") or not llm_config_experiment:
        return None

    payload = await app.EXPERIMENTATION_PROVIDER.get_payload(
        "LLM_CONFIG_BY_PROMPT_TYPE", distinct_id, properties={"organization_id": organization_id}
    )
    if not payload:
        LOG.warning(
            "No payload found for LLM config experiment",
            distinct_id=distinct_id,
            organization_id=organization_id,
            variant=llm_config_experiment,
        )
        return None

    try:
        config = json.loads(payload)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        LOG.warning(
            "Failed to parse LLM config experiment payload",
            distinct_id=distinct_id,
            organization_id=organization_id,
            variant=llm_config_experiment,
            payload=payload,
            error=str(exc),
        )
        return None

    LOG.debug(
        "LLM config by prompt type experiment enabled",
        distinct_id=distinct_id,
        organization_id=organization_id,
        variant=llm_config_experiment,
        config=config,
    )
    return config


async def get_llm_handler_for_prompt_type(
    prompt_type: str, distinct_id: str, organization_id: str | None = None
) -> LLMAPIHandler | None:
    """Return initialized handler for prompt type from LLM_CONFIG_BY_PROMPT_TYPE flag."""
    config = await get_llm_config_by_prompt_type(distinct_id, organization_id)
    if not config:
        return None
    if prompt_type not in config:
        LOG.debug(
            "Prompt type not in LLM config, using default handler",
            prompt_type=prompt_type,
            distinct_id=distinct_id,
            organization_id=organization_id,
        )
        return None

    llm_config_name = config[prompt_type]
    try:
        handler = LLMAPIHandlerFactory.get_llm_api_handler(llm_config_name)
        return handler
    except Exception:
        LOG.error(
            "Failed to initialize LLM handler for prompt type",
            prompt_type=prompt_type,
            llm_config_name=llm_config_name,
            distinct_id=distinct_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return None


# PostHog encodes a disabled multivariate flag as `False`; JS-style booleans
# can also surface as strings.
_CHECK_USER_GOAL_CONTROL_VARIANTS = {None, False, "False", "false", "", "control"}

# Failures intentionally not cached so a transient factory error doesn't
# permanently disable the experiment for the process.
_resolved_check_user_goal_handler_cache: dict[str, LLMAPIHandler] = {}
_invalid_check_user_goal_variants_logged: set[str] = set()


async def get_check_user_goal_llm_override(
    distinct_id: str, organization_id: str | None = None
) -> LLMAPIHandler | None:
    """Resolve the CHECK_USER_GOAL_LLM_NAME multivariate flag to an LLM handler."""
    try:
        variant = await app.EXPERIMENTATION_PROVIDER.get_value_cached(
            "CHECK_USER_GOAL_LLM_NAME",
            distinct_id,
            properties={"organization_id": organization_id},
        )
    except Exception:
        LOG.warning(
            "Failed to read CHECK_USER_GOAL_LLM_NAME; falling back to default handler",
            distinct_id=distinct_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return None

    if variant is None or variant in _CHECK_USER_GOAL_CONTROL_VARIANTS:
        return None

    cached = _resolved_check_user_goal_handler_cache.get(variant)
    if cached is not None:
        return cached

    try:
        handler = LLMAPIHandlerFactory.get_llm_api_handler(variant)
    except Exception:
        if variant not in _invalid_check_user_goal_variants_logged:
            LOG.warning(
                "Failed to initialize handler for CHECK_USER_GOAL_LLM_NAME variant",
                variant=variant,
                distinct_id=distinct_id,
                organization_id=organization_id,
            )
            _invalid_check_user_goal_variants_logged.add(variant)
        return None

    _resolved_check_user_goal_handler_cache[variant] = handler
    LOG.info(
        "Using CHECK_USER_GOAL_LLM_NAME override handler",
        variant=variant,
        distinct_id=distinct_id,
        organization_id=organization_id,
    )
    return handler


async def resolve_check_user_goal_handler(
    distinct_id: str,
    organization_id: str | None,
    default_handler: LLMAPIHandler,
) -> LLMAPIHandler:
    """Return CHECK_USER_GOAL_LLM_NAME override (flex-wrapped) if set; else default_handler."""
    try:
        override = await get_check_user_goal_llm_override(distinct_id, organization_id)
    except Exception:
        LOG.warning(
            "Failed to resolve CHECK_USER_GOAL_LLM_NAME; using default handler",
            distinct_id=distinct_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return default_handler

    if override is None:
        return default_handler
    return LLMAPIHandlerFactory.wrap_for_flex_routing(override)
