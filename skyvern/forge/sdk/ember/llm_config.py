from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.ember.eval_llm_overrides import eval_llm_override_handler
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type

LOG = structlog.get_logger()

EMBER_PROMPT_TYPE = "workflow-copilot"
EMBER_FAST_PROMPT_TYPE = "workflow-copilot-fast"
EMBER_RAW_SECRET_SAFETY_PROMPT_TYPE = "workflow-copilot-raw-secret-safety"


def get_ember_handler() -> Any | None:
    try:
        handler = app.EMBER_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        handler = None
    if handler is not None:
        return handler
    try:
        return app.LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        return None


def get_main_ember_handler() -> Any | None:
    try:
        handler = app.EMBER_AGENT_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        handler = None
    if handler is not None:
        return handler
    return get_ember_handler()


def get_fast_ember_handler() -> Any | None:
    try:
        handler = app.EMBER_FAST_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        handler = None
    if handler is not None:
        return handler
    try:
        return app.SECONDARY_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        return None


def get_raw_secret_safety_handler() -> LLMAPIHandler | None:
    try:
        return app.EMBER_LITE_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        return None


async def resolve_main_ember_handler(workflow_permanent_id: str | None, organization_id: str | None) -> Any | None:
    override = eval_llm_override_handler(EMBER_PROMPT_TYPE, workflow_permanent_id, organization_id)
    if override is not None:
        return override
    if workflow_permanent_id and organization_id:
        try:
            posthog_handler = await get_llm_handler_for_prompt_type(
                EMBER_PROMPT_TYPE, workflow_permanent_id, organization_id
            )
        except Exception as exc:
            LOG.warning("main copilot PostHog lookup failed, falling back", error=str(exc))
            posthog_handler = None
        if posthog_handler is not None:
            return posthog_handler
    return get_main_ember_handler()


async def resolve_ember_handler(workflow_permanent_id: str | None, organization_id: str | None) -> Any | None:
    override = eval_llm_override_handler(EMBER_PROMPT_TYPE, workflow_permanent_id, organization_id)
    if override is not None:
        return override
    if workflow_permanent_id and organization_id:
        try:
            posthog_handler = await get_llm_handler_for_prompt_type(
                EMBER_PROMPT_TYPE, workflow_permanent_id, organization_id
            )
        except Exception as exc:
            LOG.warning("workflow copilot PostHog lookup failed, falling back", error=str(exc))
            posthog_handler = None
        if posthog_handler is not None:
            return posthog_handler
    return get_ember_handler()


async def resolve_fast_ember_handler(workflow_permanent_id: str | None, organization_id: str | None) -> Any | None:
    override = eval_llm_override_handler(EMBER_FAST_PROMPT_TYPE, workflow_permanent_id, organization_id)
    if override is not None:
        return override
    if workflow_permanent_id and organization_id:
        try:
            posthog_handler = await get_llm_handler_for_prompt_type(
                EMBER_FAST_PROMPT_TYPE, workflow_permanent_id, organization_id
            )
        except Exception as exc:
            LOG.warning("fast copilot PostHog lookup failed, falling back", error=str(exc))
            posthog_handler = None
        if posthog_handler is not None:
            return posthog_handler
    return get_fast_ember_handler()


async def resolve_raw_secret_safety_handler(
    workflow_permanent_id: str | None,
    organization_id: str | None,
) -> LLMAPIHandler | None:
    override = eval_llm_override_handler(EMBER_RAW_SECRET_SAFETY_PROMPT_TYPE, workflow_permanent_id, organization_id)
    if override is not None:
        return override
    if workflow_permanent_id and organization_id:
        try:
            posthog_handler = await get_llm_handler_for_prompt_type(
                EMBER_RAW_SECRET_SAFETY_PROMPT_TYPE,
                workflow_permanent_id,
                organization_id,
            )
        except Exception as exc:
            LOG.warning("raw-secret safety PostHog lookup failed", exception_type=type(exc).__name__)
            posthog_handler = None
        if posthog_handler is not None:
            return posthog_handler
    return get_raw_secret_safety_handler()
