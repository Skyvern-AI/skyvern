"""
Service for detecting user-defined errors when tasks fail.

This module provides a centralized error detection service that can be used
by both agent execution and script execution to detect user-defined errors
based on the current page state or failure context.
"""

import asyncio
import json
from datetime import datetime

import structlog
from playwright.async_api import Page

from skyvern.errors.errors import UserDefinedError
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.services.action_service import get_action_history
from skyvern.webeye.actions.handler import extract_user_defined_errors
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()


async def detect_user_defined_errors_for_task(
    task: Task,
    step: Step,
    browser_state: BrowserState | None = None,
    failure_reason: str | None = None,
) -> list[UserDefinedError]:
    """
    Detect user-defined errors for a failed task.

    This function uses the existing extract_user_defined_errors when browser_state
    and page are available. When they're not available (early failures), it falls
    back to detecting errors from the failure context.

    Args:
        task: The task that failed
        step: The last step executed
        browser_state: Optional browser state (may be None in early failures)
        failure_reason: The reason for task failure (used when browser_state unavailable)

    Returns:
        List of detected UserDefinedError objects (empty if detection fails)
    """
    # Skip detection if no error_code_mapping defined
    if not task.error_code_mapping:
        LOG.debug(
            "No error_code_mapping defined for task, skipping error detection",
            task_id=task.task_id,
            step_id=step.step_id,
        )
        return []

    try:
        # Try to use full page-based detection if browser state is available
        if browser_state is not None:
            page = await browser_state.get_working_page()
            if page is not None:
                LOG.info(
                    "Using page-based error detection",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    url=page.url,
                )
                return await _detect_errors_from_page(task, step, page, browser_state, failure_reason)

        # Fall back to context-based detection when page is not available
        LOG.info(
            "Browser state or page not available, using context-based error detection",
            task_id=task.task_id,
            step_id=step.step_id,
            has_browser_state=browser_state is not None,
            has_failure_reason=failure_reason is not None,
        )
        return await _detect_errors_from_context(task, step, failure_reason)

    except asyncio.CancelledError:
        # Don't swallow cancellation - let it propagate
        raise
    except Exception:
        # Gracefully handle any errors during detection
        # Error detection failure should never prevent task from failing
        LOG.exception(
            "Failed to detect user-defined errors, continuing with task failure",
            task_id=task.task_id,
            step_id=step.step_id,
        )
        return []


async def _detect_errors_from_page(
    task: Task,
    step: Step,
    page: Page,
    browser_state: BrowserState,
    failure_reason: str | None,
) -> list[UserDefinedError]:
    """
    Detect errors using full page context (screenshots, HTML, action history).

    Reuses the existing extract_user_defined_errors function.
    """
    try:
        # Scrape the current page
        LOG.info(
            "Scraping page for error detection",
            task_id=task.task_id,
            step_id=step.step_id,
            url=page.url,
        )
        scraped_page = await browser_state.scrape_website(
            url=page.url,
            cleanup_element_tree=app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step),
            take_screenshots=True,
            draw_boxes=False,
        )

        if scraped_page is None:
            LOG.warning(
                "Failed to scrape page for error detection",
                task_id=task.task_id,
                step_id=step.step_id,
            )
            return []

        # Use the existing extract_user_defined_errors function
        LOG.info(
            "Calling extract_user_defined_errors with full page context",
            task_id=task.task_id,
            step_id=step.step_id,
            error_code_mapping=task.error_code_mapping,
        )
        user_defined_errors = await extract_user_defined_errors(
            task=task,
            step=step,
            scraped_page=scraped_page,
            reasoning=failure_reason,
        )

        LOG.info(
            "Detected user-defined errors from page",
            task_id=task.task_id,
            step_id=step.step_id,
            error_count=len(user_defined_errors),
            errors=[e.error_code for e in user_defined_errors],
        )

        return user_defined_errors

    except Exception:
        LOG.exception(
            "Failed to detect errors from page",
            task_id=task.task_id,
            step_id=step.step_id,
        )
        return []


async def _detect_errors_from_context(
    task: Task,
    step: Step,
    failure_reason: str | None,
) -> list[UserDefinedError]:
    """
    Detect errors using only failure context when page is not available.

    This is used for early failures (e.g., navigation failures, browser init failures)
    where we don't have access to the page or screenshots.
    """
    try:
        # Get current timezone
        context = skyvern_context.current()
        tz_info = datetime.now().astimezone().tzinfo
        if context and context.tz_info:
            tz_info = context.tz_info

        # Try to get action history even without page - may provide useful context

        action_history = []
        try:
            action_history = await get_action_history(task=task, current_step=step)
        except Exception:
            LOG.debug(
                "Could not retrieve action history for context-based detection",
                task_id=task.task_id,
                step_id=step.step_id,
            )

        # Build a degraded prompt with available context
        # Note: No screenshots, no HTML elements, but we try to include action history if available
        prompt = prompt_engine.load_prompt(
            "surface-user-defined-errors",
            navigation_goal=task.navigation_goal or "",
            navigation_payload_str=json.dumps(task.navigation_payload or {}),
            elements=[],
            current_url="",
            action_history=json.dumps(action_history),
            error_code_mapping_str=json.dumps(task.error_code_mapping),
            local_datetime=datetime.now(tz_info).isoformat(),
            reasoning=failure_reason,
        )

        # Call LLM without screenshots
        LOG.info(
            "Calling LLM to detect user-defined errors from context only",
            task_id=task.task_id,
            step_id=step.step_id,
            error_code_mapping=task.error_code_mapping,
            failure_reason=failure_reason,
        )
        json_response = await app.EXTRACTION_LLM_API_HANDLER(
            prompt=prompt,
            screenshots=[],  # No screenshots available
            step=step,
            prompt_name="surface-user-defined-errors",
        )

        # Parse and validate errors
        errors_list = json_response.get("errors", [])
        user_defined_errors = []

        for error_dict in errors_list:
            try:
                user_defined_error = UserDefinedError.model_validate(error_dict)
                user_defined_errors.append(user_defined_error)
            except Exception:
                LOG.warning(
                    "Failed to validate user-defined error",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    error_dict=error_dict,
                    exc_info=True,
                )

        LOG.info(
            "Detected user-defined errors from context",
            task_id=task.task_id,
            step_id=step.step_id,
            error_count=len(user_defined_errors),
            errors=[e.error_code for e in user_defined_errors],
        )

        return user_defined_errors

    except Exception:
        LOG.exception(
            "Failed to detect errors from context",
            task_id=task.task_id,
            step_id=step.step_id,
        )
        return []
