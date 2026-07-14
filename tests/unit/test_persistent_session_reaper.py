"""The reaper closes persistent browser sessions past their timeout so their in-process
Chromium + record_video ffmpeg encoders don't leak."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.run_enums import RunType
from skyvern.webeye import default_persistent_sessions_manager as manager_mod
from skyvern.webeye.default_persistent_sessions_manager import BrowserSession, DefaultPersistentSessionsManager

MODULE = "skyvern.webeye.default_persistent_sessions_manager"


def _make_manager(uncompleted_sessions: list, owned_ids: list[str] | None = None) -> DefaultPersistentSessionsManager:
    DefaultPersistentSessionsManager.instance = None
    DefaultPersistentSessionsManager._browser_sessions = {}
    DefaultPersistentSessionsManager._background_tasks = set()
    DefaultPersistentSessionsManager._reaper_task = None
    db = MagicMock()
    db.browser_sessions = MagicMock()
    db.browser_sessions.get_uncompleted_persistent_browser_sessions = AsyncMock(return_value=uncompleted_sessions)
    db.workflow_runs = MagicMock()
    # Default: the owning run row is gone (stale). Tests that need a live/terminal owner override this.
    db.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
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
    runnable_type: str | None = None,
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
        runnable_type=runnable_type,
    )


def _workflow_run(status: WorkflowRunStatus) -> MagicMock:
    # Real WorkflowRunStatus so .is_final() runs the production logic, not a mocked truth value.
    return MagicMock(status=status)


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
    sessions = [
        _session(
            "pbs_in_run",
            started_minutes_ago=180,
            timeout_minutes=60,
            runnable_id="wr_active",
            runnable_type=RunType.workflow_run,
        )
    ]
    manager = _make_manager(sessions)
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=_workflow_run(WorkflowRunStatus.running))
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_session_occupied_by_paused_runnable() -> None:
    # A paused workflow run still owns the session; paused is non-final, so the reaper must not close it.
    sessions = [
        _session(
            "pbs_paused",
            started_minutes_ago=180,
            timeout_minutes=60,
            runnable_id="wr_paused",
            runnable_type=RunType.workflow_run,
        )
    ]
    manager = _make_manager(sessions)
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=_workflow_run(WorkflowRunStatus.paused))
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [
        WorkflowRunStatus.completed,
        WorkflowRunStatus.failed,
        WorkflowRunStatus.terminated,
        WorkflowRunStatus.canceled,
        WorkflowRunStatus.timed_out,
    ],
)
async def test_reaps_expired_session_whose_owning_run_is_terminal(terminal_status: WorkflowRunStatus) -> None:
    # The owning run finished but died before release_browser_session cleared runnable_id. Occupancy
    # is stale, the session is past timeout+grace — the reaper must reclaim it instead of skipping forever.
    sessions = [
        _session(
            "pbs_stuck",
            started_minutes_ago=180,
            timeout_minutes=60,
            runnable_id="wr_dead",
            runnable_type=RunType.workflow_run,
        )
    ]
    manager = _make_manager(sessions)
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=_workflow_run(terminal_status))
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_awaited_once_with("org_test", "pbs_stuck")
    manager.database.workflow_runs.get_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_dead",
        organization_id="org_test",
    )


@pytest.mark.asyncio
async def test_reaps_expired_session_whose_owning_run_is_missing() -> None:
    # The owning run row is gone entirely (deleted/never findable). No live owner — reclaim the session.
    sessions = [
        _session(
            "pbs_orphan",
            started_minutes_ago=180,
            timeout_minutes=60,
            runnable_id="wr_gone",
            runnable_type=RunType.workflow_run,
        )
    ]
    manager = _make_manager(sessions)
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_awaited_once_with("org_test", "pbs_orphan")


@pytest.mark.asyncio
async def test_does_not_reap_terminal_owned_session_before_expiry() -> None:
    # Owner is terminal, but the session is still inside its timeout window. Stale ownership alone
    # must not trigger a reap — the timeout+grace gate still governs, so this is NOT reaped yet.
    sessions = [
        _session(
            "pbs_recent",
            started_minutes_ago=1,
            timeout_minutes=60,
            runnable_id="wr_dead",
            runnable_type=RunType.workflow_run,
        )
    ]
    manager = _make_manager(sessions)
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=_workflow_run(WorkflowRunStatus.completed))
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_protects_expired_session_with_unknown_runnable_type() -> None:
    # A runnable_id set with a runnable_type the reaper can't authoritatively resolve stays protected:
    # no broad age-only fallback, so this is left to its owner's teardown rather than reaped.
    sessions = [
        _session(
            "pbs_unknown",
            started_minutes_ago=180,
            timeout_minutes=60,
            runnable_id="tsk_unknown",
            runnable_type="task_v2",
        )
    ]
    manager = _make_manager(sessions)
    manager.close_session = AsyncMock()

    await manager.reap_expired_sessions()

    manager.close_session.assert_not_awaited()
    manager.database.workflow_runs.get_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_protects_expired_session_when_owner_lookup_fails() -> None:
    # If the owner lookup errors we cannot prove the run is dead, so fail safe: never reap a session
    # whose liveness is unknown (would otherwise risk killing an active run's browser).
    sessions = [
        _session(
            "pbs_dberr",
            started_minutes_ago=180,
            timeout_minutes=60,
            runnable_id="wr_dberr",
            runnable_type=RunType.workflow_run,
        )
    ]
    manager = _make_manager(sessions)
    manager.database.workflow_runs.get_workflow_run = AsyncMock(side_effect=RuntimeError("db down"))
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
async def test_reaping_stale_owned_session_closes_local_browser_state() -> None:
    # End-to-end: a stale-owned expired session must be torn down, not just marked in the DB — the
    # in-process BrowserState (Chromium + driver) is closed and dropped from the local registry.
    session = _session(
        "pbs_teardown",
        started_minutes_ago=180,
        timeout_minutes=60,
        runnable_id="wr_dead",
        runnable_type=RunType.workflow_run,
    )
    manager = _make_manager([session])
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=_workflow_run(WorkflowRunStatus.failed))
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock()

    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_artifacts = SimpleNamespace(browser_session_dir=None, video_artifacts=[])
    manager._browser_sessions["pbs_teardown"] = BrowserSession(browser_state=browser_state, organization_id="org_test")

    with patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"):
        await manager.reap_expired_sessions()

    browser_state.close.assert_awaited_once()
    assert "pbs_teardown" not in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_teardown",
        "org_test",
    )


@pytest.mark.asyncio
async def test_reap_pass_survives_close_failure_without_dropping_session() -> None:
    # A teardown/DB failure while reaping one session must not abort the pass or silently drop the
    # session: the exception is contained, and the row stays uncompleted so a later pass retries it.
    session = _session(
        "pbs_flaky_close",
        started_minutes_ago=180,
        timeout_minutes=60,
        runnable_id="wr_dead",
        runnable_type=RunType.workflow_run,
    )
    manager = _make_manager([session])
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=_workflow_run(WorkflowRunStatus.completed))
    manager.close_session = AsyncMock(side_effect=RuntimeError("close failed"))

    await manager.reap_expired_sessions()  # must not raise

    manager.close_session.assert_awaited_once_with("org_test", "pbs_flaky_close")


@pytest.mark.asyncio
async def test_reap_is_idempotent_after_session_reclaimed() -> None:
    # After a stale-owned session is reaped its BrowserState is popped from the local registry, so a
    # later pass (even if the DB row is still returned) hits the not-held guard and does not re-tear
    # it down. Repeated reaps never double-close a session.
    session = _session(
        "pbs_idem",
        started_minutes_ago=180,
        timeout_minutes=60,
        runnable_id="wr_dead",
        runnable_type=RunType.workflow_run,
    )
    manager = _make_manager([session])
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=_workflow_run(WorkflowRunStatus.completed))
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock()

    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_artifacts = SimpleNamespace(browser_session_dir=None, video_artifacts=[])
    manager._browser_sessions["pbs_idem"] = BrowserSession(browser_state=browser_state, organization_id="org_test")

    with patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"):
        await manager.reap_expired_sessions()
        await manager.reap_expired_sessions()

    browser_state.close.assert_awaited_once()
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_idem",
        "org_test",
    )


@pytest.mark.asyncio
async def test_reclaims_cdp_connect_request_level_session_after_run_dies() -> None:
    # Production wiring: a run submitted with browser_session_id under cdp-connect occupies the session
    # via begin_session(runnable_type="workflow_run", runnable_id=workflow_run_id). If that run dies
    # without releasing, the reaper resolves the same workflow_run and reclaims the expired session.
    assert RunType.workflow_run == "workflow_run"  # the exact literal begin_session writes
    session = _session(
        "pbs_request_level",
        started_minutes_ago=180,
        timeout_minutes=60,
        runnable_id="wr_request_level",
        runnable_type=RunType.workflow_run,
    )
    manager = _make_manager([session])
    manager.database.workflow_runs.get_workflow_run = AsyncMock(
        return_value=_workflow_run(WorkflowRunStatus.terminated)
    )
    manager.close_session = AsyncMock()

    with (
        patch.object(manager_mod.settings, "BROWSER_TYPE", "cdp-connect"),
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
    ):
        await manager.reap_expired_sessions()

    manager.close_session.assert_awaited_once_with("org_test", "pbs_request_level")
    manager.database.workflow_runs.get_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_request_level",
        organization_id="org_test",
    )


@pytest.mark.asyncio
async def test_start_reaper_is_noop_when_no_in_process_browsers() -> None:
    # Neither trigger for an in-process browser launch: nothing to reap, so don't start the loop.
    manager = _make_manager([])
    with patch(f"{MODULE}.settings") as mock_settings:
        mock_settings.BROWSER_STREAMING_MODE = "vnc"
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
