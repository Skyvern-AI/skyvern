"""The reaper closes persistent browser sessions past their timeout so their in-process
Chromium + record_video ffmpeg encoders don't leak."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.run_enums import RunType
from skyvern.webeye import default_persistent_sessions_manager as manager_mod
from skyvern.webeye.default_persistent_sessions_manager import BrowserSession, DefaultPersistentSessionsManager
from skyvern.webeye.vnc_manager import VncManager, VncTeardownError

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


# ---------------------------------------------------------------------------
# reconcile_local_sessions — reclaim worker-local state when another replica
# completes/closes the shared DB row (which reap_expired_sessions never revisits
# because it only scans uncompleted rows).
# ---------------------------------------------------------------------------


def _completed_row(session_id: str, org: str = "org_test") -> MagicMock:
    row = MagicMock(
        persistent_browser_session_id=session_id,
        organization_id=org,
        completed_at=datetime.now(timezone.utc),
        status="completed",
        runnable_id=None,
    )
    # Opted in by default so reconcile's export-verdict path is exercised; opt-out tests override this.
    row.should_export_profile.return_value = True
    return row


def _active_row(session_id: str, org: str = "org_test", status: str = "running") -> MagicMock:
    return MagicMock(
        persistent_browser_session_id=session_id,
        organization_id=org,
        completed_at=None,
        status=status,
        runnable_id=None,
    )


def _hold_local_session(
    manager: DefaultPersistentSessionsManager,
    session_id: str,
    org: str = "org_test",
    *,
    real_state: bool = False,
) -> MagicMock:
    """Register a BrowserState this process holds in _browser_sessions."""
    if real_state:
        browser_state = MagicMock()
        browser_state.close = AsyncMock()
        # Skip the profile-export/video branches so these tests isolate resource release.
        browser_state.browser_artifacts = SimpleNamespace(browser_session_dir=None, video_artifacts=[])
    else:
        browser_state = MagicMock()
    manager._browser_sessions[session_id] = BrowserSession(browser_state=browser_state, organization_id=org)
    return browser_state


@pytest.mark.asyncio
async def test_reconcile_reclaims_local_state_for_completed_row() -> None:
    # Another replica completed the shared row; this process still holds the BrowserState.
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_done")
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_completed_row("pbs_done")
    )
    manager._release_local_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_awaited_once_with("org_test", "pbs_done", export_profile=True)


@pytest.mark.asyncio
async def test_reconcile_leaves_active_uncompleted_row_untouched() -> None:
    # The authoritative row is still active/renewable — ordinary expiration owns it, not reconcile.
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_active")
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=_active_row("pbs_active"))
    manager._release_local_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_not_awaited()
    assert "pbs_active" in manager._browser_sessions


@pytest.mark.asyncio
async def test_reconcile_leaves_completed_row_with_a_live_owning_run() -> None:
    # A terminal row that still carries a runnable_id whose owning run is still live belongs to that
    # run's own teardown — never yank a browser out from under a running task/workflow.
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_in_run")
    occupied = _completed_row("pbs_in_run")
    occupied.runnable_id = "wr_active"
    occupied.runnable_type = RunType.workflow_run
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=occupied)
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=_workflow_run(WorkflowRunStatus.running))
    manager._release_local_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_not_awaited()
    assert "pbs_in_run" in manager._browser_sessions


@pytest.mark.asyncio
async def test_reconcile_reclaims_completed_row_whose_owning_run_is_dead() -> None:
    # close_persistent_browser_session leaves runnable_id set, and a completed row is invisible to
    # reap_expired_sessions — so a completed row whose owning workflow_run is terminal/missing would
    # leak forever if reconcile skipped it unconditionally. Resolve the owner like reap does and
    # reclaim once it is gone.
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_dead_owner")
    row = _completed_row("pbs_dead_owner")
    row.runnable_id = "wr_dead"
    row.runnable_type = RunType.workflow_run
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=row)
    manager.database.workflow_runs.get_workflow_run = AsyncMock(return_value=None)  # owner gone
    manager._release_local_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_awaited_once_with("org_test", "pbs_dead_owner", export_profile=True)


@pytest.mark.asyncio
async def test_reconcile_protects_completed_row_with_unknown_owner_type() -> None:
    # An owner we can't authoritatively resolve (unrecognized runnable type) is treated as active, so
    # reconcile never reclaims a session we can't prove is unowned — same fail-safe as the reaper.
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_unknown_owner")
    row = _completed_row("pbs_unknown_owner")
    row.runnable_id = "task_1"
    row.runnable_type = "task_run"
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=row)
    manager._release_local_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_not_awaited()
    assert "pbs_unknown_owner" in manager._browser_sessions


@pytest.mark.asyncio
async def test_reconcile_reclaims_missing_row_without_a_second_db_close() -> None:
    # A None row means the shared session was soft-deleted / is gone. Reclaim the orphaned local
    # state, but NEVER route through the DB close (it raises NotFoundError on a missing row).
    manager = _make_manager([], owned_ids=[])
    browser_state = _hold_local_session(manager, "pbs_gone", real_state=True)
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=None)
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()

    browser_state.close.assert_awaited_once()
    assert "pbs_gone" not in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("row_present", [True, False])
async def test_vnc_reconcile_finishes_local_teardown_under_lock_without_a_second_db_close(
    row_present: bool,
) -> None:
    manager = _make_manager([], owned_ids=[])
    browser_state = _hold_local_session(manager, "pbs_vnc_done", real_state=True)
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_completed_row("pbs_vnc_done") if row_present else None
    )
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock()
    session_lock = manager._get_session_lock("pbs_vnc_done")
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def close_browser() -> None:
        assert session_lock.locked()
        close_started.set()
        await release_close.wait()

    async def stop_vnc(*_args: object, **_kwargs: object) -> None:
        assert session_lock.locked()

    browser_state.close.side_effect = close_browser

    with (
        patch(f"{MODULE}.settings.BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock(side_effect=stop_vnc)) as stop_vnc_mock,
    ):
        reconcile = asyncio.create_task(manager.reconcile_local_sessions())
        await close_started.wait()
        reconcile.cancel()
        await asyncio.sleep(0)
        reconcile.cancel()
        await asyncio.sleep(0)
        assert not reconcile.done()
        assert "pbs_vnc_done" in manager._browser_sessions

        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await reconcile

    browser_state.close.assert_awaited_once()
    stop_vnc_mock.assert_awaited_once_with("pbs_vnc_done", organization_id="org_test")
    assert "pbs_vnc_done" not in manager._browser_sessions
    assert not session_lock.locked()
    manager.database.browser_sessions.close_persistent_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_vnc_reconcile_retains_cache_after_stop_failure_then_retries() -> None:
    manager = _make_manager([], owned_ids=[])
    browser_state = _hold_local_session(manager, "pbs_vnc_retry", real_state=True)
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_completed_row("pbs_vnc_retry")
    )
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock()
    teardown_error = VncTeardownError("pbs_vnc_retry", survivors=("Xvfb",))

    with (
        patch(f"{MODULE}.settings.BROWSER_STREAMING_MODE", "vnc"),
        patch.object(
            VncManager,
            "stop_vnc_for_session",
            new=AsyncMock(side_effect=[teardown_error, teardown_error, None]),
        ) as stop_vnc,
    ):
        await manager.reconcile_local_sessions()
        assert "pbs_vnc_retry" in manager._browser_sessions

        await manager.reconcile_local_sessions()

    assert browser_state.close.await_count == 2
    assert stop_vnc.await_count == 3
    assert "pbs_vnc_retry" not in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_preserves_local_state_on_db_lookup_error_then_retries() -> None:
    # A transient DB read must not tear down a session whose true state is unknown; the next pass retries.
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_flaky")
    manager._release_local_browser_session = AsyncMock()
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        side_effect=RuntimeError("db unreachable")
    )

    await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_not_awaited()
    assert "pbs_flaky" in manager._browser_sessions

    # Next pass: the DB is reachable and the row is authoritatively completed — now reclaim it.
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_completed_row("pbs_flaky")
    )
    await manager.reconcile_local_sessions()
    manager._release_local_browser_session.assert_awaited_once_with("org_test", "pbs_flaky", export_profile=True)


@pytest.mark.asyncio
async def test_reconcile_is_idempotent_across_duplicate_passes() -> None:
    # Two overlapping/duplicate passes must close the browser exactly once and not error on the empty pass.
    manager = _make_manager([], owned_ids=[])
    browser_state = _hold_local_session(manager, "pbs_done", real_state=True)
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_completed_row("pbs_done")
    )
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()
    await manager.reconcile_local_sessions()

    browser_state.close.assert_awaited_once()
    assert "pbs_done" not in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_handles_mixed_states_independently() -> None:
    # One completed + one active local session reconcile independently: reclaim the done one, keep the live one.
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_done")
    _hold_local_session(manager, "pbs_live")
    rows = {"pbs_done": _completed_row("pbs_done"), "pbs_live": _active_row("pbs_live")}

    def fake_get(session_id: str, organization_id: str | None = None) -> MagicMock:
        return rows[session_id]

    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(side_effect=fake_get)
    manager._release_local_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_awaited_once_with("org_test", "pbs_done", export_profile=True)
    assert "pbs_live" in manager._browser_sessions


@pytest.mark.asyncio
async def test_reconcile_preserves_session_with_active_copilot_turn() -> None:
    # An active copilot turn is a live, local "in use now" signal — do not reclaim it even if the DB row
    # reads completed; the next pass reclaims once the copilot registry clears it.
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_copilot")
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_completed_row("pbs_copilot")
    )
    manager._release_local_browser_session = AsyncMock()

    with patch(f"{MODULE}.active_copilot_session_ids", return_value={"pbs_copilot"}):
        await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_not_awaited()
    assert "pbs_copilot" in manager._browser_sessions


@pytest.mark.asyncio
async def test_reconcile_skips_session_with_unknown_organization() -> None:
    # Without a known org we can't do the authoritative org-scoped lookup, so fail safe: don't touch
    # the local state and don't even query. (In practice org is always populated for cdp-connect/PBS.)
    manager = _make_manager([], owned_ids=[])
    _hold_local_session(manager, "pbs_no_org", org=None)
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_completed_row("pbs_no_org")
    )
    manager._release_local_browser_session = AsyncMock()

    await manager.reconcile_local_sessions()

    manager._release_local_browser_session.assert_not_awaited()
    manager.database.browser_sessions.get_persistent_browser_session.assert_not_awaited()
    assert "pbs_no_org" in manager._browser_sessions


def _hold_exportable_session(
    manager: DefaultPersistentSessionsManager, session_id: str, org: str = "org_test"
) -> MagicMock:
    """Hold a session whose browser_state has a profile dir, so the profile-export path actually runs."""
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_context = MagicMock()
    browser_state.browser_artifacts = SimpleNamespace(browser_session_dir=f"/tmp/{session_id}", video_artifacts=[])
    manager._browser_sessions[session_id] = BrowserSession(browser_state=browser_state, organization_id=org)
    return browser_state


@pytest.mark.asyncio
async def test_reconcile_missing_row_tears_down_without_exporting_profile() -> None:
    # Privacy fail-closed: a soft-deleted / gone row (None) can't confirm the profile opt-in, so
    # reconcile must release the local state WITHOUT uploading the profile dir/cookies — otherwise a
    # default opted-out session's data would be persisted just because its row was deleted.
    manager = _make_manager([], owned_ids=[])
    browser_state = _hold_exportable_session(manager, "pbs_gone")
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=None)
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()) as persist_cookies,
    ):
        await manager.reconcile_local_sessions()

    storage.store_browser_profile.assert_not_awaited()
    persist_cookies.assert_not_awaited()
    browser_state.close.assert_awaited_once()
    assert "pbs_gone" not in manager._browser_sessions


@pytest.mark.asyncio
async def test_reconcile_present_opted_in_row_still_exports_profile() -> None:
    # A present terminal row that opted in must still export on reclaim — the missing-row fail-closed
    # guard must not suppress a legitimate opted-in export.
    manager = _make_manager([], owned_ids=[])
    browser_state = _hold_exportable_session(manager, "pbs_opt_in")
    row = _completed_row("pbs_opt_in")
    row.should_export_profile.return_value = True
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=row)
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()),
    ):
        await manager.reconcile_local_sessions()

    storage.store_browser_profile.assert_awaited_once()
    browser_state.close.assert_awaited_once()
    assert "pbs_opt_in" not in manager._browser_sessions


@pytest.mark.asyncio
async def test_reconcile_present_opted_out_row_does_not_export_profile() -> None:
    # A present terminal row that opted out skips export (same as close_session) while still being
    # reclaimed — the opt-in flag on the present row is honored, no export.
    manager = _make_manager([], owned_ids=[])
    browser_state = _hold_exportable_session(manager, "pbs_opt_out")
    row = _completed_row("pbs_opt_out")
    row.should_export_profile.return_value = False
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=row)
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()),
    ):
        await manager.reconcile_local_sessions()

    storage.store_browser_profile.assert_not_awaited()
    browser_state.close.assert_awaited_once()
    assert "pbs_opt_out" not in manager._browser_sessions


@pytest.mark.asyncio
async def test_reconcile_resolves_export_verdict_from_a_single_read() -> None:
    # reconcile resolves the profile opt-in from its own authoritative read and passes that verdict to
    # _release_local_browser_session, which then issues NO second get_persistent_browser_session. That
    # single-read contract is what removes the soft-delete race a second read would open — there is no
    # window for the row to change between reads. Prove exactly one lookup for the correct session/org,
    # no export off an opted-out row, and teardown still happening.
    manager = _make_manager([], owned_ids=[])
    browser_state = _hold_exportable_session(manager, "pbs_single_read")
    row = _completed_row("pbs_single_read")
    row.should_export_profile.return_value = False
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=row)
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()),
    ):
        await manager.reconcile_local_sessions()

    manager.database.browser_sessions.get_persistent_browser_session.assert_awaited_once_with(
        "pbs_single_read", "org_test"
    )
    storage.store_browser_profile.assert_not_awaited()
    browser_state.close.assert_awaited_once()
    assert "pbs_single_read" not in manager._browser_sessions


@pytest.mark.asyncio
async def test_reap_misses_cross_pod_completed_row_but_reconcile_reclaims_it() -> None:
    # The core bug: reap_expired_sessions only scans uncompleted rows, so a row another replica already
    # completed is invisible to it — its local BrowserState leaks. reconcile_local_sessions catches it.
    manager = _make_manager([], owned_ids=[])  # get_uncompleted returns [] (row completed elsewhere)
    browser_state = _hold_local_session(manager, "pbs_xpod", real_state=True)
    manager.database.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=_completed_row("pbs_xpod")
    )
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock()
    manager.close_session = AsyncMock()

    # reap alone can't see it: the completed row isn't in the uncompleted scan.
    await manager.reap_expired_sessions()
    manager.close_session.assert_not_awaited()
    assert "pbs_xpod" in manager._browser_sessions

    # reconcile reclaims the orphaned local state.
    await manager.reconcile_local_sessions()

    browser_state.close.assert_awaited_once()
    assert "pbs_xpod" not in manager._browser_sessions


@pytest.mark.asyncio
async def test_reaper_loop_runs_reconcile_after_reap_even_when_reap_fails() -> None:
    # Wiring: each reaper pass runs reconcile after reap, and a reap failure must not skip reconcile.
    manager = _make_manager([], owned_ids=[])
    manager.reap_expired_sessions = AsyncMock(side_effect=RuntimeError("reap boom"))
    manager.reconcile_local_sessions = AsyncMock()

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with patch(f"{MODULE}.asyncio.sleep", sleep_mock):
        with pytest.raises(asyncio.CancelledError):
            await manager._reap_expired_sessions_loop(1)

    manager.reap_expired_sessions.assert_awaited_once()
    manager.reconcile_local_sessions.assert_awaited_once()
