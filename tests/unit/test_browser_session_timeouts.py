from skyvern.schemas.browser_session_timeouts import (
    MAX_LIFETIME_SECONDS,
    seconds_until_expiry,
    session_is_active,
)

_BASE = 60 * 60  # 1h base timeout
_IDLE = 60 * 60  # idle out 1h after last activity
_CAP = MAX_LIFETIME_SECONDS


def _active(**overrides: float | None) -> bool:
    kwargs: dict[str, float | None] = {
        "seconds_since_start": 0.0,
        "base_timeout_seconds": _BASE,
        "seconds_since_last_activity": None,
        "idle_timeout_seconds": _IDLE,
        "max_lifetime_seconds": _CAP,
    }
    kwargs.update(overrides)
    return session_is_active(**kwargs)  # type: ignore[arg-type]


def test_within_base_timeout_is_active_without_activity() -> None:
    assert _active(seconds_since_start=_BASE - 1, seconds_since_last_activity=None) is True


def test_past_base_timeout_without_activity_is_inactive() -> None:
    # No activity ever recorded -> pre-activity behavior: dies at base timeout.
    assert _active(seconds_since_start=_BASE + 1, seconds_since_last_activity=None) is False


def test_base_timeout_boundary_is_inactive_without_activity() -> None:
    assert _active(seconds_since_start=_BASE, seconds_since_last_activity=None) is False


def test_past_base_timeout_with_recent_activity_stays_active() -> None:
    assert _active(seconds_since_start=_BASE * 5, seconds_since_last_activity=_IDLE - 1) is True


def test_past_base_timeout_with_stale_activity_is_inactive() -> None:
    assert _active(seconds_since_start=_BASE * 5, seconds_since_last_activity=_IDLE + 1) is False


def test_idle_boundary_is_inactive() -> None:
    assert _active(seconds_since_start=_BASE * 5, seconds_since_last_activity=_IDLE) is False


def test_hard_cap_overrides_recent_activity() -> None:
    # Actively driven, but past the 24h lifetime cap -> reaped regardless.
    assert _active(seconds_since_start=_CAP, seconds_since_last_activity=0.0) is False


def test_just_under_hard_cap_with_activity_stays_active() -> None:
    assert _active(seconds_since_start=_CAP - 1, seconds_since_last_activity=0.0) is True


def test_future_activity_timestamp_counts_as_recent() -> None:
    # Clock skew can make last_activity slightly ahead of now -> negative elapsed.
    # Treated as very recent (active), never as stale.
    assert _active(seconds_since_start=_BASE * 5, seconds_since_last_activity=-5.0) is True


def test_default_cap_is_max_timeout() -> None:
    # Omitting max_lifetime_seconds falls back to the 24h MAX_TIMEOUT ceiling.
    assert (
        session_is_active(
            seconds_since_start=_CAP,
            base_timeout_seconds=_BASE,
            seconds_since_last_activity=0.0,
            idle_timeout_seconds=_IDLE,
        )
        is False
    )


def test_seconds_until_expiry_uses_the_later_base_or_activity_deadline() -> None:
    assert (
        seconds_until_expiry(
            seconds_since_start=_BASE + 120,
            base_timeout_seconds=_BASE,
            seconds_since_last_activity=120,
            idle_timeout_seconds=_IDLE,
        )
        == _IDLE - 120
    )


def test_seconds_until_expiry_is_capped_by_max_lifetime() -> None:
    assert (
        seconds_until_expiry(
            seconds_since_start=_CAP - 30,
            base_timeout_seconds=_BASE,
            seconds_since_last_activity=0,
            idle_timeout_seconds=_IDLE,
        )
        == 30
    )


def test_session_is_active_matches_positive_remaining_time() -> None:
    cases = (
        (_BASE - 1, None),
        (_BASE, None),
        (_BASE * 5, _IDLE - 1),
        (_BASE * 5, _IDLE),
        (_CAP, 0),
    )

    for seconds_since_start, seconds_since_last_activity in cases:
        remaining = seconds_until_expiry(
            seconds_since_start=seconds_since_start,
            base_timeout_seconds=_BASE,
            seconds_since_last_activity=seconds_since_last_activity,
            idle_timeout_seconds=_IDLE,
        )
        assert _active(
            seconds_since_start=seconds_since_start,
            seconds_since_last_activity=seconds_since_last_activity,
        ) is (remaining > 0)
