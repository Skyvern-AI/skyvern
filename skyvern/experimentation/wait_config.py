"""
Wait time optimization experiment configuration.

This module provides configurable wait times that can be controlled via PostHog feature flags.
Allows for A/B testing of different wait time strategies to optimize speed while maintaining success rates.
"""

import json
from typing import Any

import structlog

from skyvern.forge import app

LOG = structlog.get_logger()


# Preset variant configurations (all times in seconds)
WAIT_VARIANTS = {
    "baseline": {
        "post_click_delay": 0.3,
        "post_input_dropdown_delay": 3.0,
        "dropdown_close_wait": 2.0,
        "checkbox_retry_delay": 2.0,
        "scroll_into_view_wait": 2.0,
        "empty_page_retry_wait": 3.0,
        "mouse_movement_delay_min": 0.2,
        "mouse_movement_delay_max": 0.3,
    },
    "moderate": {
        "post_click_delay": 0.15,
        "post_input_dropdown_delay": 1.5,
        "dropdown_close_wait": 1.0,
        "checkbox_retry_delay": 1.0,
        "scroll_into_view_wait": 1.0,
        "empty_page_retry_wait": 2.0,
        "mouse_movement_delay_min": 0.1,
        "mouse_movement_delay_max": 0.15,
    },
    "aggressive": {
        "post_click_delay": 0.05,
        "post_input_dropdown_delay": 0.5,
        "dropdown_close_wait": 0.5,
        "checkbox_retry_delay": 0.5,
        "scroll_into_view_wait": 0.5,
        "empty_page_retry_wait": 1.0,
        "mouse_movement_delay_min": 0.05,
        "mouse_movement_delay_max": 0.1,
    },
}


class WaitConfig:
    """Manages wait time configuration with PostHog experiment support."""

    def __init__(self, payload: dict[str, Any] | None = None):
        """
        Initialize wait config from PostHog payload.

        Expected payload format:
        {
            "variant": "moderate",  # optional preset variant
            "overrides": {          # optional specific overrides
                "post_click_delay": 0.15,
                ...
            },
            "global_multiplier": 1.0,  # optional multiplier for all waits
            "adaptive_mode": false,     # optional adaptive retry behavior
            "adaptive_backoff": 1.5     # optional backoff multiplier
        }
        """
        self.variant = "baseline"
        self.overrides: dict[str, float] = {}
        self.global_multiplier = 1.0
        self.adaptive_mode = False
        self.adaptive_backoff = 1.5

        if payload:
            self._load_from_payload(payload)

    def _load_from_payload(self, payload: dict[str, Any]) -> None:
        """Load configuration from PostHog payload."""
        self.variant = payload.get("variant", "baseline")
        self.overrides = payload.get("overrides", {})
        self.global_multiplier = payload.get("global_multiplier", 1.0)
        self.adaptive_mode = payload.get("adaptive_mode", False)
        self.adaptive_backoff = payload.get("adaptive_backoff", 1.5)

        # Validate variant
        if self.variant not in WAIT_VARIANTS:
            LOG.warning(
                "Invalid wait variant, falling back to baseline",
                variant=self.variant,
                valid_variants=list(WAIT_VARIANTS.keys()),
            )
            self.variant = "baseline"

    def get_wait_time(self, wait_type: str, retry_count: int = 0) -> float:
        """
        Get wait time for a specific wait type.

        Args:
            wait_type: Type of wait (e.g., "post_click_delay")
            retry_count: Current retry count (for adaptive mode)

        Returns:
            Wait time in seconds (clamped to [0.0, 30.0])
        """
        # Check for override first
        if wait_type in self.overrides:
            base_wait = self.overrides[wait_type]
        else:
            # Get from variant preset
            variant_config = WAIT_VARIANTS.get(self.variant, WAIT_VARIANTS["baseline"])
            base_wait = variant_config.get(wait_type, 0.0)

        # Sanitize inputs to prevent negative/extreme waits from payload misconfig
        base_wait = max(0.0, float(base_wait))
        multiplier = max(0.0, float(self.global_multiplier))
        backoff = max(0.0, float(self.adaptive_backoff))

        # Apply global multiplier
        wait_time = base_wait * multiplier

        # Apply adaptive backoff if enabled
        if self.adaptive_mode and retry_count > 0:
            wait_time *= backoff**retry_count

        # Cap at 30 seconds to prevent extreme waits from exponential backoff
        return min(30.0, max(0.0, wait_time))

    def get_mouse_movement_delay_range(self) -> tuple[float, float]:
        """Get min and max for mouse movement delays."""
        min_delay = self.get_wait_time("mouse_movement_delay_min")
        max_delay = self.get_wait_time("mouse_movement_delay_max")
        return (min_delay, max_delay)

    def to_dict(self) -> dict[str, Any]:
        """Export current configuration as dict for logging."""
        return {
            "variant": self.variant,
            "overrides": self.overrides,
            "global_multiplier": self.global_multiplier,
            "adaptive_mode": self.adaptive_mode,
            "adaptive_backoff": self.adaptive_backoff,
        }


async def get_wait_config_from_experiment(
    distinct_id: str,
    organization_id: str,
) -> WaitConfig | None:
    """
    Get wait configuration from PostHog experiment.

    Args:
        distinct_id: Unique identifier for experiment assignment
        organization_id: Organization ID for experiment properties

    Returns:
        WaitConfig instance if experiment is active, None otherwise
    """

    if not app.EXPERIMENTATION_PROVIDER:
        return None

    # Check if user is in the experiment
    wait_optimization_experiment = await app.EXPERIMENTATION_PROVIDER.get_value_cached(
        "WAIT_TIME_OPTIMIZATION", distinct_id, properties={"organization_id": organization_id}
    )

    # Skip if user is in control group (False or "False") or experiment is disabled (None/empty)
    if wait_optimization_experiment in (False, "False") or not wait_optimization_experiment:
        return None

    # If we have an active variant, get the payload
    payload = await app.EXPERIMENTATION_PROVIDER.get_payload_cached(
        "WAIT_TIME_OPTIMIZATION", distinct_id, properties={"organization_id": organization_id}
    )

    if payload:
        try:
            config_dict = json.loads(payload) if isinstance(payload, str) else payload
            wait_config = WaitConfig(config_dict)
            LOG.info(
                "Wait time optimization experiment enabled",
                distinct_id=distinct_id,
                organization_id=organization_id,
                variant=wait_optimization_experiment,
                config=wait_config.to_dict(),
            )
            return wait_config
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            LOG.warning(
                "Failed to parse wait optimization experiment payload",
                distinct_id=distinct_id,
                variant=wait_optimization_experiment,
                payload=payload,
                error=str(e),
            )
    else:
        LOG.warning(
            "No payload found for wait optimization experiment",
            distinct_id=distinct_id,
            variant=wait_optimization_experiment,
        )

    return None
