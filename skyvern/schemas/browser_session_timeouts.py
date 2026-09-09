"""Timeout and liveness policy for persistent browser sessions.

Kept dependency-free on purpose: the CDP proxy connect gate and the session worker
share these rules without pulling the heavier ``browser_sessions`` schema module
(workflow/recording types) into their import graphs.
"""

from __future__ import annotations

from typing import overload

MIN_TIMEOUT = 5
# The most a session can be created with.
MAX_TIMEOUT = 60 * 4  # 4 hours
MAX_LIFETIME_SECONDS = MAX_TIMEOUT * 60
# The most a session can be extended to after creation, across every extension it receives.
MAX_EXTENDED_TIMEOUT = 60 * 6  # 6 hours
MAX_EXTENDED_LIFETIME_SECONDS = MAX_EXTENDED_TIMEOUT * 60
# A session closer than this to its deadline is not renewed: the session worker polls the budget on a
# 30s heartbeat and must observe the extension before it decides to shut down.
RENEWAL_MIN_REMAINING_SECONDS = 60
# The public extend endpoint is stricter: its 200 is a promise, and the worker keeps its last-known
# budget through a failed poll, so leave room for more than one failed heartbeat.
EXTENSION_MIN_REMAINING_SECONDS = 120
# A found reusable session with less lifetime left than this is retired and replaced rather than handed to a run.
REUSE_MIN_REMAINING_LIFETIME_SECONDS = 30 * 60
DEFAULT_TIMEOUT = 60

MAX_TIMEOUT_EXCEEDED_MESSAGE = (
    "Longer browser durations are available on our enterprise plan, please contact sales@skyvern.com"
)
MAX_LIFETIME_REACHED_MESSAGE = (
    f"the session is already at the maximum lifetime of {MAX_EXTENDED_TIMEOUT} minutes "
    f"({MAX_EXTENDED_TIMEOUT // 60} hours). {MAX_TIMEOUT_EXCEEDED_MESSAGE}"
)


def max_timeout_exceeded_warning(requested_timeout_minutes: int) -> str:
    """Warning returned when a create request asked for more than the cap allows."""
    return (
        f"Requested timeout of {requested_timeout_minutes} minutes exceeds the maximum of "
        f"{MAX_TIMEOUT} minutes ({MAX_TIMEOUT // 60} hours); this session was capped at "
        f"{MAX_TIMEOUT} minutes. A running session can be extended up to {MAX_EXTENDED_TIMEOUT} minutes "
        f"({MAX_EXTENDED_TIMEOUT // 60} hours) with the extend endpoint. {MAX_TIMEOUT_EXCEEDED_MESSAGE}"
    )


def max_lifetime_exceeded_warning(requested_additional_minutes: int, granted_minutes: int) -> str:
    """Warning returned when an extend request asked for more than the remaining headroom."""
    return (
        f"Requested an extension of {requested_additional_minutes} minutes, but only {granted_minutes} minutes "
        f"remained under the maximum session lifetime of {MAX_EXTENDED_TIMEOUT} minutes "
        f"({MAX_EXTENDED_TIMEOUT // 60} hours); this session was extended by {granted_minutes} minutes. "
        f"{MAX_TIMEOUT_EXCEEDED_MESSAGE}"
    )


@overload
def creation_timeout_minutes(requested_minutes: int) -> int: ...
@overload
def creation_timeout_minutes(requested_minutes: None) -> None: ...
@overload
def creation_timeout_minutes(requested_minutes: int | None) -> int | None: ...
def creation_timeout_minutes(requested_minutes: int | None) -> int | None:
    """The budget a session may be created with: the request, capped at ``MAX_TIMEOUT``.

    Every creator goes through this, not only the public route, so a budget above the creation cap can only
    ever come from an extension. That is what lets the gates treat an oversized budget as evidence of one.
    """
    if requested_minutes is None:
        return None
    return min(requested_minutes, MAX_TIMEOUT)


def lifetime_cap_seconds(base_timeout_seconds: float, *, floor_seconds: float = MAX_LIFETIME_SECONDS) -> float:
    """The hard cap on an extendable session's life.

    ``floor_seconds`` is the cap for a session that was never extended. A budget above it can only come from
    an extension (creation is capped at ``MAX_TIMEOUT`` by ``creation_timeout_minutes``), so the cap follows
    the budget up, never past ``MAX_EXTENDED_LIFETIME_SECONDS``.
    """
    return max(floor_seconds, min(base_timeout_seconds, MAX_EXTENDED_LIFETIME_SECONDS))


def seconds_until_expiry(
    *,
    seconds_since_start: float,
    base_timeout_seconds: float,
    seconds_since_last_activity: float | None,
    idle_timeout_seconds: float,
    max_lifetime_seconds: float = MAX_LIFETIME_SECONDS,
    activity_extends_deadline: bool = True,
    budget_may_lift_cap: bool = False,
) -> float:
    """Remaining lifetime under the base, activity, and hard-cap deadlines.

    Activity carries a session past its base timeout only where a later deadline can actually be
    served. Infrastructure that pins a session's end at provisioning serves no later one, so
    counting its activity lease reports time the browser will not be alive for (SKY-15044).

    ``max_lifetime_seconds`` is the hard cap for a session that was never extended. With
    ``budget_may_lift_cap`` a budget above it raises the cap with it, up to the extended maximum.
    Only first-party infrastructure can serve an extension, so a caller opts in only where the
    session is known to run there; everywhere else an oversized budget buys nothing past the cap.
    """
    lease_remaining_seconds = base_timeout_seconds - seconds_since_start
    if activity_extends_deadline and seconds_since_last_activity is not None:
        lease_remaining_seconds = max(
            lease_remaining_seconds,
            idle_timeout_seconds - seconds_since_last_activity,
        )
    hard_cap_seconds = (
        lifetime_cap_seconds(base_timeout_seconds, floor_seconds=max_lifetime_seconds)
        if budget_may_lift_cap
        else max_lifetime_seconds
    )
    return min(lease_remaining_seconds, hard_cap_seconds - seconds_since_start)


def session_is_active(
    *,
    seconds_since_start: float,
    base_timeout_seconds: float,
    seconds_since_last_activity: float | None,
    idle_timeout_seconds: float,
    max_lifetime_seconds: float = MAX_LIFETIME_SECONDS,
    budget_may_lift_cap: bool = False,
) -> bool:
    """Whether a persistent browser session should still be treated as alive.

    A session lives until its base timeout; past that it stays alive only while it
    keeps seeing activity (every client CDP command refreshes its last-activity mark),
    idling out ``idle_timeout_seconds`` after the last one. A hard lifetime cap
    overrides both so an actively-driven session cannot be renewed forever; only an
    explicit extension lifts that cap, never past ``MAX_EXTENDED_LIFETIME_SECONDS``, and
    only where the caller passes ``budget_may_lift_cap`` for first-party infrastructure.

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
            budget_may_lift_cap=budget_may_lift_cap,
        )
        > 0
    )
