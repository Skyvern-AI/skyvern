from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type

LOG = structlog.get_logger()

WORKFLOW_COPILOT_PROMPT_TYPE = "workflow-copilot"
WORKFLOW_COPILOT_FAST_PROMPT_TYPE = "workflow-copilot-fast"
WORKFLOW_COPILOT_LITE_PROMPT_TYPE = "workflow-copilot-lite"


def get_workflow_copilot_handler() -> Any | None:
    try:
        handler = app.WORKFLOW_COPILOT_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        handler = None
    if handler is not None:
        return handler
    try:
        return app.LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        return None


def get_main_copilot_handler() -> Any | None:
    try:
        handler = app.WORKFLOW_COPILOT_AGENT_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        handler = None
    if handler is not None:
        return handler
    return get_workflow_copilot_handler()


def get_fast_copilot_handler() -> Any | None:
    try:
        handler = app.WORKFLOW_COPILOT_FAST_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        handler = None
    if handler is not None:
        return handler
    try:
        return app.SECONDARY_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        return None


async def resolve_main_copilot_handler(workflow_permanent_id: str | None, organization_id: str | None) -> Any | None:
    if workflow_permanent_id and organization_id:
        try:
            posthog_handler = await get_llm_handler_for_prompt_type(
                WORKFLOW_COPILOT_PROMPT_TYPE, workflow_permanent_id, organization_id
            )
        except Exception as exc:
            LOG.warning("main copilot PostHog lookup failed, falling back", error=str(exc))
            posthog_handler = None
        if posthog_handler is not None:
            return posthog_handler
    return get_main_copilot_handler()


async def resolve_workflow_copilot_handler(
    workflow_permanent_id: str | None, organization_id: str | None
) -> Any | None:
    if workflow_permanent_id and organization_id:
        try:
            posthog_handler = await get_llm_handler_for_prompt_type(
                WORKFLOW_COPILOT_PROMPT_TYPE, workflow_permanent_id, organization_id
            )
        except Exception as exc:
            LOG.warning("workflow copilot PostHog lookup failed, falling back", error=str(exc))
            posthog_handler = None
        if posthog_handler is not None:
            return posthog_handler
    return get_workflow_copilot_handler()


async def resolve_fast_copilot_handler(workflow_permanent_id: str | None, organization_id: str | None) -> Any | None:
    if workflow_permanent_id and organization_id:
        try:
            posthog_handler = await get_llm_handler_for_prompt_type(
                WORKFLOW_COPILOT_FAST_PROMPT_TYPE, workflow_permanent_id, organization_id
            )
        except Exception as exc:
            LOG.warning("fast copilot PostHog lookup failed, falling back", error=str(exc))
            posthog_handler = None
        if posthog_handler is not None:
            return posthog_handler
    return get_fast_copilot_handler()


async def resolve_lite_copilot_handler(workflow_permanent_id: str | None, organization_id: str | None) -> Any | None:
    if workflow_permanent_id and organization_id:
        try:
            posthog_handler = await get_llm_handler_for_prompt_type(
                WORKFLOW_COPILOT_LITE_PROMPT_TYPE, workflow_permanent_id, organization_id
            )
        except Exception as exc:
            LOG.warning("lite copilot PostHog lookup failed, falling back", error=str(exc))
            posthog_handler = None
        if posthog_handler is not None:
            return posthog_handler
    return await resolve_workflow_copilot_handler(workflow_permanent_id, organization_id)
