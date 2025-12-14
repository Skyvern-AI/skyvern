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

    LOG.info(
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
    if not config or prompt_type not in config:
        LOG.warning(
            "No config found for prompt type",
            prompt_type=prompt_type,
            config=config,
            distinct_id=distinct_id,
            organization_id=organization_id,
        )
        return None

    llm_config_name = config[prompt_type]
    try:
        handler = LLMAPIHandlerFactory.get_llm_api_handler(llm_config_name)
        LOG.info(
            "Using LLM handler for prompt type from experiment",
            prompt_type=prompt_type,
            llm_config_name=llm_config_name,
            distinct_id=distinct_id,
            organization_id=organization_id,
        )
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
