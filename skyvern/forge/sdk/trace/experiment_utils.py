"""Utilities for collecting and formatting experiment data for tracing."""

from typing import TYPE_CHECKING, Any

import structlog

from skyvern.forge.sdk.core import skyvern_context

if TYPE_CHECKING:
    from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider

LOG = structlog.get_logger()


async def collect_experiment_metadata_safely(
    experimentation_provider: "BaseExperimentationProvider",
) -> dict[str, Any]:
    """
    Safely collect experiment-related metadata from the current context.

    This is a safe wrapper around collect_experiment_metadata() that ensures
    any exceptions are caught and handled gracefully.

    Args:
        experimentation_provider: The experimentation provider to use for fetching experiment data.

    Returns:
        Dictionary containing experiment data, or empty dict if collection fails.
    """
    try:
        return await collect_experiment_metadata(experimentation_provider)
    except Exception:
        LOG.warning("Failed to collect experiment metadata", exc_info=True)
        return {}


async def collect_experiment_metadata(
    experimentation_provider: "BaseExperimentationProvider",
) -> dict[str, Any]:
    """
    Collect experiment-related metadata from the current context.

    Args:
        experimentation_provider: The experimentation provider to use for fetching experiment data.

    Returns:
        Dictionary containing experiment data that can be added to traces.
    """
    # Get the current context
    context = skyvern_context.current()
    if not context or not context.run_id:
        return {}

    # Use run_id as the distinct_id for experiments
    distinct_id = context.run_id
    organization_id = context.organization_id

    if not distinct_id or not organization_id:
        return {}

    experiment_metadata: dict[str, Any] = {}

    try:
        # Only collect critical experiment flags that are relevant for tracing
        experiment_flags = [
            "LLM_NAME",
            "LLM_SECONDARY_NAME",
            # Add more experiment flags as needed
            "PROMPT_CACHING_OPTIMIZATION",
            "THINKING_BUDGET_OPTIMIZATION",
        ]

        for flag in experiment_flags:
            try:
                # Get the experiment value (already cached by experimentation provider)
                value = await experimentation_provider.get_value_cached(
                    flag, distinct_id, properties={"organization_id": organization_id}
                )

                # Get the payload if available (already cached by experimentation provider)
                payload = await experimentation_provider.get_payload_cached(
                    flag, distinct_id, properties={"organization_id": organization_id}
                )

                # Only include if we have actual experiment data
                if value is not None or payload is not None:
                    experiment_metadata[f"experiment_{flag}"] = {"value": value, "payload": payload}

            except Exception:
                # Silently skip failed experiments
                continue

    except Exception:
        # Silently fail if experimentation provider is not available
        pass

    return experiment_metadata
