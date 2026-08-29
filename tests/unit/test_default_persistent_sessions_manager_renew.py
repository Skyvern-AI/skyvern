"""``renew_session`` must never shorten a session's expiry. Its extension is computed as
``(now + DEBUG_SESSION_TIMEOUT_MINUTES) - current_expiry``, which goes negative whenever a
session's remaining lifetime already exceeds ``DEBUG_SESSION_TIMEOUT_MINUTES`` -- e.g. Copilot's
30-minute sessions (SKY-15165).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    PersistentBrowserSession,
    PersistentBrowserSessionStatus,
)
from skyvern.webeye import default_persistent_sessions_manager as manager_module
from skyvern.webeye.default_persistent_sessions_manager import renew_session


def _session(*, timeout_minutes: int, minutes_left: float) -> PersistentBrowserSession:
    now = datetime.now(timezone.utc)
    started_at = now + timedelta(minutes=minutes_left) - timedelta(minutes=timeout_minutes)
    return PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        status=PersistentBrowserSessionStatus.running,
        timeout_minutes=timeout_minutes,
        started_at=started_at,
        completed_at=None,
        created_at=started_at,
        modified_at=started_at,
    )


def _database(session: PersistentBrowserSession) -> MagicMock:
    database = MagicMock()
    database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=session)
    database.browser_sessions.update_persistent_browser_session = AsyncMock()
    return database


@pytest.mark.asyncio
async def test_renewal_never_shortens_a_session_with_ample_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    # 29 minutes left on a 30-minute session: a fresh (now + 20min) window is earlier than the
    # existing expiry, so the old code subtracted 9 minutes off the session instead of extending it.
    session = _session(timeout_minutes=30, minutes_left=29)
    database = _database(session)
    log_info = MagicMock()
    monkeypatch.setattr(manager_module.LOG, "info", log_info)

    result = await renew_session(database, session.persistent_browser_session_id, session.organization_id)

    assert result.timeout_minutes == 30
    database.browser_sessions.update_persistent_browser_session.assert_not_called()
    assert not any(
        call.kwargs.get("lifecycle_event") == "browser_session_timeout_extended" for call in log_info.call_args_list
    )


def test_renewal_extension_minutes_never_goes_negative() -> None:
    now = datetime.now(timezone.utc)

    # New window lands before the current expiry: would-be shorten clamps to a no-op.
    assert manager_module._renewal_extension_minutes(now, now + timedelta(minutes=9)) == 0
    # New window lands exactly on the current expiry: no-op.
    assert manager_module._renewal_extension_minutes(now, now) == 0
    # New window lands after the current expiry: genuine extension, unclamped.
    assert manager_module._renewal_extension_minutes(now + timedelta(minutes=5), now) == 5


@pytest.mark.asyncio
async def test_a_longer_lived_session_still_renews_within_the_threshold() -> None:
    # 15 minutes left, above DEBUG_SESSION_TIMEOUT_THRESHOLD_MINUTES (10) but below
    # DEBUG_SESSION_TIMEOUT_MINUTES (20): a fresh 20-minute window is later than the current
    # expiry, so this must genuinely extend the session.
    session = _session(timeout_minutes=30, minutes_left=15)
    database = _database(session)
    extended = session.model_copy(update={"timeout_minutes": 35})
    database.browser_sessions.update_persistent_browser_session.return_value = extended

    result = await renew_session(database, session.persistent_browser_session_id, session.organization_id)

    assert result.timeout_minutes > 30
    database.browser_sessions.update_persistent_browser_session.assert_called_once()
    _, kwargs = database.browser_sessions.update_persistent_browser_session.call_args
    assert kwargs["timeout_minutes"] > 30


def test_debug_session_timeout_defaults_assumed_by_this_test_module() -> None:
    # These scenarios are only meaningful for the threshold/extension window this bug was filed
    # against; if the defaults ever change, the minute values above need re-deriving.
    assert settings.DEBUG_SESSION_TIMEOUT_MINUTES == 20
    assert settings.DEBUG_SESSION_TIMEOUT_THRESHOLD_MINUTES == 10
