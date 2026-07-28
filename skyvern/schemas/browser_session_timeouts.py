"""Timeout and liveness policy for persistent browser sessions.

Kept dependency-free on purpose: the CDP proxy connect gate and the session worker
share these rules without pulling the heavier ``browser_sessions`` schema module
(workflow/recording types) into their import graphs.
"""

from __future__ import annotations

MIN_TIMEOUT = 5
MAX_TIMEOUT = 60 * 24  # 24 hours
MAX_LIFETIME_SECONDS = MAX_TIMEOUT * 60
DEFAULT_TIMEOUT = 60


def seconds_until_expiry(
    *,
    seconds_since_start: float,
    base_timeout_seconds: float,
    seconds_since_last_activity: float | None,
    idle_timeout_seconds: float,
    max_lifetime_seconds: float = MAX_LIFETIME_SECONDS,
) -> float:
    """Remaining lifetime under the base, activity, and hard-cap deadlines."""
    lease_remaining_seconds = base_timeout_seconds - seconds_since_start
    if seconds_since_last_activity is not None:
        lease_remaining_seconds = max(
            lease_remaining_seconds,
            idle_timeout_seconds - seconds_since_last_activity,
        )
    hard_cap_remaining_seconds = max_lifetime_seconds - seconds_since_start
    return min(lease_remaining_seconds, hard_cap_remaining_seconds)


def session_is_active(
    *,
    seconds_since_start: float,
    base_timeout_seconds: float,
    seconds_since_last_activity: float | None,
    idle_timeout_seconds: float,
    max_lifetime_seconds: float = MAX_LIFETIME_SECONDS,
) -> bool:
    """Whether a persistent browser session should still be treated as alive.

    A session lives until its base timeout; past that it stays alive only while it
    keeps seeing activity (every client CDP command refreshes its last-activity mark),
    idling out ``idle_timeout_seconds`` after the last one. A hard lifetime cap
    overrides both so an actively-driven session cannot be renewed forever.

    Inputs are elapsed durations in seconds so the wall-clock connect gate and the
    monotonic worker loop can share one policy. ``seconds_since_last_activity`` is
    None when the session has never recorded activity, in which case only the base
    timeout keeps it alive — preserving the pre-activity behavior exactly.
    """
    return (
        seconds_until_expiry(
            seconds_since_start=seconds_since_start,
            base_timeout_seconds=base_timeout_seconds,
            seconds_since_last_activity=seconds_since_last_activity,
            idle_timeout_seconds=idle_timeout_seconds,
            max_lifetime_seconds=max_lifetime_seconds,
        )
        > 0
    )
