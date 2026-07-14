"""The reaper closes persistent browser sessions past their timeout so their in-process
Chromium + record_video ffmpeg encoders don't leak."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager

MODULE = "skyvern.webeye.default_persistent_sessions_manager"


def _make_manager(uncompleted_sessions: list, owned_ids: list[str] | None = None) -> DefaultPersistentSessionsManager:
    DefaultPersistentSessionsManager.instance = None
    DefaultPersistentSessionsManager._browser_sessions = {}
    DefaultPersistentSessionsManager._background_tasks = set()
    DefaultPersistentSessionsManager._reaper_task = None
    db = MagicMock()
    db.browser_sessions = MagicMock()
    db.browser_sessions.get_uncompleted_persistent_browser_sessions = AsyncMock(return_value=uncompleted_sessions)
    manager = DefaultPersistentSessionsManager(database=db)
    # Register the browsers this process "holds" — the reaper only touches these.
    held = owned_ids if owned_ids is not None else [s.persistent_browser_session_id for s in uncompleted_sessions]
    for session_id in held:
        manager._browser_sessions[session_id] = MagicMock()
    return manager


def _session(
    session_id: str,
    started_minutes_ago: float | None,
    timeout_minutes: int | None,
    runnable_id: str | None = None,
) -> MagicMock:
    started_at = None
    if started_minutes_ago is not None:
        started_at = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
    return MagicMock(
        persistent_browser_session_id=session_id,
        organization_id="org_test",
        started_at=started_at,
        timeout_minutes=timeout_minutes,
        runnable_id=runnable_id,
    )


@pytest.mark.asyncio
async def test_reaps_only_sessions_past_timeout_and_grace() -> None:
    sessions = [
        _session("pbs_expired", started_minutes_ago=30, timeout_minutes=20),  # expired ~10m ago
        _session("pbs_fresh", started_minutes_ago=1, timeout_minutes=20),  # ~19m left
        _session("pbs_unstarted", started_minutes_ago=None, timeout_minutes=20),  # still launching
    ]
    manager = _make_manager(sessions)
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_awaited_once_with("org_test", "pbs_expired")


@pytest.mark.asyncio
async def test_grace_margin_protects_just_expired_session() -> None:
    # Expired right at its timeout (~0s ago) — inside the grace window, so it must NOT be reaped yet.
    sessions = [_session("pbs_just_expired", started_minutes_ago=20, timeout_minutes=20)]
    manager = _make_manager(sessions)
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_expired_session_not_held_by_this_process() -> None:
    # Expired, but this process doesn't hold its browser — another process owns the teardown, so
    # completing the row here would hide that owner's leak. Leave it alone.
    sessions = [_session("pbs_other_process", started_minutes_ago=30, timeout_minutes=20)]
    manager = _make_manager(sessions, owned_ids=[])
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_session_occupied_by_running_runnable() -> None:
    # Past timeout and held here, but still occupied by a running workflow (renewal caps at 2h
    # while runs can go longer). Its run owns teardown, so the reaper must not close it.
    sessions = [_session("pbs_in_run", started_minutes_ago=180, timeout_minutes=60, runnable_id="wr_active")]
    manager = _make_manager(sessions)
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_session_held_by_active_copilot_turn() -> None:
    # Past timeout and held here, but an active copilot turn is driving it (copilot sessions have no
    # runnable_id and aren't renewed). The registry marks it in-use, so the reaper must not close it.
    sessions = [_session("pbs_copilot", started_minutes_ago=40, timeout_minutes=30)]
    manager = _make_manager(sessions)
    manager.close_session = AsyncMock()

    with patch(f"{MODULE}.active_copilot_session_ids", return_value={"pbs_copilot"}):
        await manager.reap_expired_sessions()

    manager.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_reaper_is_noop_when_no_in_process_browsers() -> None:
    # Neither trigger for an in-process browser launch: nothing to reap, so don't start the loop.
    manager = _make_manager([])
    with patch(f"{MODULE}.settings") as mock_settings:
        mock_settings.BROWSER_STREAMING_MODE = "other"
        mock_settings.BROWSER_TYPE = "chromium-headful"
        mock_settings.PERSISTENT_SESSIONS_REAPER_INTERVAL_SECONDS = 60
        manager.start_reaper()
    assert manager._reaper_task is None


@pytest.mark.asyncio
async def test_start_reaper_is_noop_when_interval_disabled() -> None:
    manager = _make_manager([])
    with patch(f"{MODULE}.settings") as mock_settings:
        mock_settings.BROWSER_STREAMING_MODE = "cdp"
        mock_settings.BROWSER_TYPE = "chromium-headful"
        mock_settings.PERSISTENT_SESSIONS_REAPER_INTERVAL_SECONDS = 0
        manager.start_reaper()
    assert manager._reaper_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "streaming_mode, browser_type",
    [
        ("cdp", "chromium-headful"),  # cdp streaming launches in-process browsers
        ("vnc", "chromium-headful"),  # vnc streaming owns Chromium plus its VNC process stack
        ("vnc", "cdp-connect"),  # cdp-connect launches even without cdp streaming
    ],
)
async def test_start_reaper_starts_once_when_in_process_browsers_launch(streaming_mode: str, browser_type: str) -> None:
    manager = _make_manager([])
    with patch(f"{MODULE}.settings") as mock_settings:
        mock_settings.BROWSER_STREAMING_MODE = streaming_mode
        mock_settings.BROWSER_TYPE = browser_type
        mock_settings.PERSISTENT_SESSIONS_REAPER_INTERVAL_SECONDS = 60
        manager.start_reaper()
        first_task = manager._reaper_task
        manager.start_reaper()  # idempotent: must not spawn a second loop
        assert manager._reaper_task is first_task

    assert first_task is not None
    first_task.cancel()
    try:
        await first_task
    except BaseException:
        pass


@pytest.mark.asyncio
async def test_reaper_treats_vnc_stack_as_process_local_ownership() -> None:
    sessions = [_session("pbs_vnc_only", started_minutes_ago=30, timeout_minutes=20)]
    manager = _make_manager(sessions, owned_ids=[])
    manager.close_session = AsyncMock()

    with (
        patch(f"{MODULE}.settings.BROWSER_STREAMING_MODE", "vnc"),
        patch(f"{MODULE}.VncManager.has_session", return_value=True),
    ):
        await manager.reap_expired_sessions()

    manager.close_session.assert_awaited_once_with("org_test", "pbs_vnc_only")


@pytest.mark.asyncio
async def test_cdp_reaper_does_not_consult_vnc_ownership() -> None:
    sessions = [_session("pbs_cdp_other", started_minutes_ago=30, timeout_minutes=20)]
    manager = _make_manager(sessions, owned_ids=[])
    manager.close_session = AsyncMock()

    with (
        patch(f"{MODULE}.settings.BROWSER_STREAMING_MODE", "cdp"),
        patch(f"{MODULE}.VncManager.has_session", return_value=True) as has_vnc_session,
    ):
        await manager.reap_expired_sessions()

    has_vnc_session.assert_not_called()
    manager.close_session.assert_not_awaited()
