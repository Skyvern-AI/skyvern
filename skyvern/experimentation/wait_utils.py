"""
Wait utility functions for configurable waits.

These functions wrap asyncio.sleep with experiment-aware wait times and metrics tracking.
"""

import asyncio

import structlog
from cachetools import TTLCache

from skyvern.experimentation.wait_config import WaitConfig, get_wait_config_from_experiment

LOG = structlog.get_logger()

# Global cache for wait config per task/workflow
# TTL of 1 hour ensures cache doesn't grow unbounded in long-running processes
# Most tasks/workflows complete within minutes, so 1 hour TTL is safe
_wait_config_cache: TTLCache[str, WaitConfig | None] = TTLCache(maxsize=10000, ttl=3600)


async def get_or_create_wait_config(
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
) -> WaitConfig | None:
    """
    Get or create wait config for a task/workflow.

    Uses caching to avoid repeated PostHog calls within the same task/workflow.

    Args:
        task_id: Task ID for experiment assignment and caching
        workflow_run_id: Workflow run ID for experiment assignment and caching
        organization_id: Organization ID for experiment properties

    Returns None if:
    - ENABLE_WAIT_TIME_OPTIMIZATION_EXPERIMENT is False (killswitch activated)
    - organization_id is None
    - No experiment is active for this task/workflow
    """
    # KILLSWITCH: Check if experiment is disabled globally (lazy import to avoid circular dependency)
    try:
        from cloud.config import settings as cloud_settings  # noqa: PLC0415

        if not cloud_settings.ENABLE_WAIT_TIME_OPTIMIZATION_EXPERIMENT:
            return None
    except ImportError:
        # If cloud.config isn't available (OSS), experiment is disabled by default
        return None

    if not organization_id:
        return None

    # Use task_id or workflow_run_id as cache key
    cache_key = task_id or workflow_run_id
    if not cache_key:
        return None

    # Check cache first
    if cache_key in _wait_config_cache:
        return _wait_config_cache[cache_key]

    # Get from experiment
    wait_config = await get_wait_config_from_experiment(cache_key, organization_id)

    # Cache it
    _wait_config_cache[cache_key] = wait_config

    return wait_config


# Simple helper for getting wait time from config
def get_wait_time(
    wait_config: WaitConfig | None,
    wait_type: str,
    default: float,
    retry_count: int = 0,
) -> float:
    """
    Get wait time from config or use default.

    Args:
        wait_config: WaitConfig instance or None
        wait_type: Type of wait (e.g., "post_click_delay")
        default: Default wait time in seconds (HARDCODED PRODUCTION VALUE)
        retry_count: Current retry count (for adaptive mode)

    Returns:
        Wait time in seconds
    """
    if wait_config:
        return wait_config.get_wait_time(wait_type, retry_count)
    return default


# Convenience functions for common wait patterns


async def scroll_into_view_wait(
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
) -> None:
    """
    Wait after scrolling element into view.

    Note: This is called from low-level DOM utilities (SkyvernElement.scroll_into_view)
    which don't have task context available. Threading context through would require
    invasive changes to many call sites. Defaults are reasonable for this utility function.
    """
    wait_config = await get_or_create_wait_config(task_id, workflow_run_id, organization_id)
    wait_seconds = get_wait_time(wait_config, "scroll_into_view_wait", default=2.0)
    await asyncio.sleep(wait_seconds)


async def empty_page_retry_wait(
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
) -> None:
    """
    Wait before retrying scrape when no elements found.

    Note: This is called from the scraper (scrape_web_unsafe) which doesn't have task context.
    Threading context through would require updating scrape_website, scrape_web_unsafe, and all
    their callers. Defaults are reasonable for this low-level scraping utility.
    """
    wait_config = await get_or_create_wait_config(task_id, workflow_run_id, organization_id)
    wait_seconds = get_wait_time(wait_config, "empty_page_retry_wait", default=3.0)
    await asyncio.sleep(wait_seconds)
