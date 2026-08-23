from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_routes
from skyvern.schemas.browser_session_timeouts import (
    DEFAULT_TIMEOUT,
    MAX_LIFETIME_SECONDS,
    MAX_TIMEOUT,
    MAX_TIMEOUT_EXCEEDED_MESSAGE,
    MIN_TIMEOUT,
    max_timeout_exceeded_warning,
    seconds_until_expiry,
    session_is_active,
)
from skyvern.schemas.browser_sessions import CreateBrowserSessionRequest
from skyvern.webeye.schemas import BrowserSessionResponse

_BASE = 60 * 60  # 1h base timeout
_IDLE = 60 * 60  # idle out 1h after last activity
_CAP = MAX_LIFETIME_SECONDS
# Past the base timeout but well under the lifetime cap, so activity-renewal cases
# are decided by activity rather than by the cap.
_PAST_BASE = _BASE * 2


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
    assert _active(seconds_since_start=_PAST_BASE, seconds_since_last_activity=_IDLE - 1) is True


def test_past_base_timeout_with_stale_activity_is_inactive() -> None:
    assert _active(seconds_since_start=_PAST_BASE, seconds_since_last_activity=_IDLE + 1) is False


def test_idle_boundary_is_inactive() -> None:
    assert _active(seconds_since_start=_PAST_BASE, seconds_since_last_activity=_IDLE) is False


def test_hard_cap_overrides_recent_activity() -> None:
    # Actively driven, but past the lifetime cap -> reaped regardless.
    assert _active(seconds_since_start=_CAP, seconds_since_last_activity=0.0) is False


def test_just_under_hard_cap_with_activity_stays_active() -> None:
    assert _active(seconds_since_start=_CAP - 1, seconds_since_last_activity=0.0) is True


def test_future_activity_timestamp_counts_as_recent() -> None:
    # Clock skew can make last_activity slightly ahead of now -> negative elapsed.
    # Treated as very recent (active), never as stale.
    assert _active(seconds_since_start=_PAST_BASE, seconds_since_last_activity=-5.0) is True


def test_default_cap_is_max_timeout() -> None:
    # Omitting max_lifetime_seconds falls back to the 4h MAX_TIMEOUT ceiling.
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
        (_PAST_BASE, _IDLE - 1),
        (_PAST_BASE, _IDLE),
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


async def _create_session(timeout: int | None) -> tuple[MagicMock, BrowserSessionResponse]:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(
        return_value=SimpleNamespace(persistent_browser_session_id="pbs_1")
    )
    built_response = BrowserSessionResponse(
        browser_session_id="pbs_1",
        organization_id="org_1",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )
    from_browser_session = AsyncMock(return_value=built_response)

    with (
        patch.object(browser_sessions_routes, "app", app_mock),
        patch.object(browser_sessions_routes.BrowserSessionResponse, "from_browser_session", from_browser_session),
    ):
        response = await browser_sessions_routes.create_browser_session(
            CreateBrowserSessionRequest(timeout=timeout),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    return app_mock, response


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [MAX_TIMEOUT + 1, 1440])
async def test_requested_timeout_above_the_cap_is_capped_with_a_warning(timeout: int) -> None:
    app_mock, response = await _create_session(timeout)

    assert app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.await_args.kwargs["timeout_minutes"] == MAX_TIMEOUT
    assert response.warning == max_timeout_exceeded_warning(timeout)
    assert str(timeout) in response.warning
    assert MAX_TIMEOUT_EXCEEDED_MESSAGE in response.warning


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [MAX_TIMEOUT, 90, None])
async def test_requested_timeout_within_the_cap_is_passed_through_without_a_warning(timeout: int | None) -> None:
    app_mock, response = await _create_session(timeout)

    assert app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.await_args.kwargs["timeout_minutes"] == timeout
    assert response.warning is None


def test_requested_timeout_at_or_below_the_cap_is_preserved() -> None:
    assert CreateBrowserSessionRequest(timeout=MAX_TIMEOUT).timeout == MAX_TIMEOUT
    assert CreateBrowserSessionRequest(timeout=MIN_TIMEOUT).timeout == MIN_TIMEOUT
    assert CreateBrowserSessionRequest(timeout=90).timeout == 90


def test_requested_timeout_below_the_minimum_is_still_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateBrowserSessionRequest(timeout=MIN_TIMEOUT - 1)


def test_timeout_defaults_and_explicit_none_are_untouched() -> None:
    assert CreateBrowserSessionRequest().timeout == DEFAULT_TIMEOUT
    assert CreateBrowserSessionRequest(timeout=None).timeout is None
