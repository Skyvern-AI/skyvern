"""Timeout and liveness policy for persistent browser sessions.

Kept dependency-free on purpose: the CDP proxy connect gate and the session worker
share these rules without pulling the heavier ``browser_sessions`` schema module
(workflow/recording types) into their import graphs.
"""

from __future__ import annotations

MIN_TIMEOUT = 5
MAX_TIMEOUT = 60 * 4  # 4 hours
MAX_LIFETIME_SECONDS = MAX_TIMEOUT * 60
# A found reusable session with less lifetime left than this is retired and replaced rather than handed to a run.
REUSE_MIN_REMAINING_LIFETIME_SECONDS = 30 * 60
DEFAULT_TIMEOUT = 60

MAX_TIMEOUT_EXCEEDED_MESSAGE = (
    "Longer browser durations are available on our enterprise plan, please contact sales@skyvern.com"
)


def max_timeout_exceeded_warning(requested_timeout_minutes: int) -> str:
    """Warning returned when a create request asked for more than the cap allows."""
    return (
        f"Requested timeout of {requested_timeout_minutes} minutes exceeds the maximum of "
        f"{MAX_TIMEOUT} minutes ({MAX_TIMEOUT // 60} hours); this session was capped at "
        f"{MAX_TIMEOUT} minutes. {MAX_TIMEOUT_EXCEEDED_MESSAGE}"
    )


def seconds_until_expiry(
    *,
    seconds_since_start: float,
    base_timeout_seconds: float,
    seconds_since_last_activity: float | None,
    idle_timeout_seconds: float,
    max_lifetime_seconds: float = MAX_LIFETIME_SECONDS,
    activity_extends_deadline: bool = True,
) -> float:
    """Remaining lifetime under the base, activity, and hard-cap deadlines.

    Activity carries a session past its base timeout only where a later deadline can actually be
    served. Infrastructure that pins a session's end at provisioning serves no later one, so
    counting its activity lease reports time the browser will not be alive for (SKY-15044).
    """
    lease_remaining_seconds = base_timeout_seconds - seconds_since_start
    if activity_extends_deadline and seconds_since_last_activity is not None:
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
