from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSessionStatus
from skyvern.webeye import default_persistent_sessions_manager as manager_module
from skyvern.webeye.default_persistent_sessions_manager import BrowserSession, DefaultPersistentSessionsManager
from skyvern.webeye.real_browser_manager import RealBrowserManager
from skyvern.webeye.vnc_manager import VncManager


@pytest.fixture
def manager() -> DefaultPersistentSessionsManager:
    DefaultPersistentSessionsManager.instance = None
    DefaultPersistentSessionsManager._browser_sessions = {}
    DefaultPersistentSessionsManager._background_tasks = set()
    DefaultPersistentSessionsManager._reaper_task = None
    DefaultPersistentSessionsManager._session_locks = {}
    db = MagicMock()
    db.browser_sessions = MagicMock()
    db.browser_sessions.create_persistent_browser_session = AsyncMock()
    db.browser_sessions.update_persistent_browser_session = AsyncMock()
    db.browser_sessions.get_persistent_browser_session = AsyncMock()
    db.browser_sessions.get_all_active_persistent_browser_sessions = AsyncMock(return_value=[])
    return DefaultPersistentSessionsManager(database=db)


def session_row(session_id: str = "pbs_1", **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "persistent_browser_session_id": session_id,
        "organization_id": "org_1",
        "status": PersistentBrowserSessionStatus.created,
        "completed_at": None,
        "proxy_location": None,
        "proxy_session_id": None,
        "browser_profile_id": None,
        "display_number": 100,
        "vnc_port": 6080,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_create_vnc_session_starts_and_persists_stack_for_runnable_session(
    manager: DefaultPersistentSessionsManager,
) -> None:
    created = session_row(display_number=None, vnc_port=None)
    persisted = session_row(display_number=100, vnc_port=6080)
    manager.database.browser_sessions.create_persistent_browser_session.return_value = created
    manager.database.browser_sessions.update_persistent_browser_session.return_value = persisted

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module.settings, "BROWSER_TYPE", "chromium-headful"),
        patch.object(VncManager, "start_vnc_for_session", new=AsyncMock(return_value=(100, 6080))) as start_vnc,
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()),
        patch.object(manager, "_launch_browser_for_session", new=AsyncMock()) as launch_browser,
    ):
        result = await manager.create_session(
            organization_id="org_1",
            proxy_location=None,
            runnable_id="wr_1",
            runnable_type="workflow_run",
        )

    assert result is persisted
    start_vnc.assert_awaited_once_with("pbs_1", organization_id="org_1")
    manager.database.browser_sessions.update_persistent_browser_session.assert_awaited_once_with(
        "pbs_1",
        organization_id="org_1",
        display_number=100,
        vnc_port=6080,
    )
    launch_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_standalone_vnc_session_schedules_browser_launch(
    manager: DefaultPersistentSessionsManager,
) -> None:
    created = session_row(display_number=None, vnc_port=None)
    persisted = session_row(display_number=100, vnc_port=6080)
    manager.database.browser_sessions.create_persistent_browser_session.return_value = created
    manager.database.browser_sessions.update_persistent_browser_session.return_value = persisted

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module.settings, "BROWSER_TYPE", "chromium-headful"),
        patch.object(VncManager, "start_vnc_for_session", new=AsyncMock(return_value=(100, 6080))),
        patch.object(manager, "_launch_browser_for_session", new=AsyncMock()) as launch_browser,
    ):
        await manager.create_session(
            organization_id="org_1",
            proxy_location=None,
            url="https://example.com",
        )
        await asyncio.gather(*list(manager._background_tasks))

    launch_browser.assert_awaited_once_with("pbs_1", "org_1", None, "https://example.com")


@pytest.mark.asyncio
async def test_vnc_start_failure_finalizes_created_row_without_masking_error(
    manager: DefaultPersistentSessionsManager,
) -> None:
    created = session_row(display_number=None, vnc_port=None)
    manager.database.browser_sessions.create_persistent_browser_session.return_value = created
    primary_error = RuntimeError("xvfb did not start")
    manager.update_status = AsyncMock(return_value=session_row(status=PersistentBrowserSessionStatus.failed))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "start_vnc_for_session", new=AsyncMock(side_effect=primary_error)),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        with pytest.raises(RuntimeError) as exc_info:
            await manager.create_session(organization_id="org_1")

    assert exc_info.value is primary_error
    stop_vnc.assert_awaited_once_with("pbs_1", organization_id="org_1")
    manager.update_status.assert_awaited_once_with("pbs_1", "org_1", PersistentBrowserSessionStatus.failed)


@pytest.mark.asyncio
async def test_metadata_persistence_failure_stops_stack_and_finalizes_row(
    manager: DefaultPersistentSessionsManager,
) -> None:
    created = session_row(display_number=None, vnc_port=None)
    manager.database.browser_sessions.create_persistent_browser_session.return_value = created
    primary_error = RuntimeError("metadata write failed")
    manager.database.browser_sessions.update_persistent_browser_session.side_effect = primary_error
    manager.update_status = AsyncMock(return_value=session_row(status=PersistentBrowserSessionStatus.failed))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "start_vnc_for_session", new=AsyncMock(return_value=(100, 6080))),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        with pytest.raises(RuntimeError) as exc_info:
            await manager.create_session(organization_id="org_1")

    assert exc_info.value is primary_error
    stop_vnc.assert_awaited_once_with("pbs_1", organization_id="org_1")
    manager.update_status.assert_awaited_once_with("pbs_1", "org_1", PersistentBrowserSessionStatus.failed)


@pytest.mark.asyncio
async def test_cleanup_failures_do_not_mask_metadata_persistence_error(
    manager: DefaultPersistentSessionsManager,
) -> None:
    created = session_row(display_number=None, vnc_port=None)
    manager.database.browser_sessions.create_persistent_browser_session.return_value = created
    primary_error = RuntimeError("metadata write failed")
    manager.database.browser_sessions.update_persistent_browser_session.side_effect = primary_error
    manager.update_status = AsyncMock(side_effect=RuntimeError("status write failed"))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "start_vnc_for_session", new=AsyncMock(return_value=(100, 6080))),
        patch.object(
            VncManager,
            "stop_vnc_for_session",
            new=AsyncMock(side_effect=RuntimeError("stop failed")),
        ),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            await manager.create_session(organization_id="org_1")

    assert exc_info.value is primary_error


@pytest.mark.asyncio
async def test_standalone_browser_launch_receives_persisted_display(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    browser_state = MagicMock()
    browser_state.get_or_create_page = AsyncMock()
    browser_state.close = AsyncMock()

    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))
    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(return_value=browser_state)) as create,
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()),
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    assert create.await_args.kwargs["display_number"] == 104


@pytest.mark.asyncio
async def test_standalone_vnc_launch_fails_closed_without_persisted_display(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=None, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock()) as create,
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    create.assert_not_awaited()
    stop_vnc.assert_awaited_once_with("pbs_1", organization_id="org_1")
    manager.update_status.assert_awaited_once_with("pbs_1", "org_1", PersistentBrowserSessionStatus.failed)


@pytest.mark.asyncio
async def test_standalone_cdp_launch_omits_display_kwarg(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    browser_state = MagicMock()
    browser_state.get_or_create_page = AsyncMock()
    browser_state.close = AsyncMock()
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "cdp"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(return_value=browser_state)) as create,
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    assert "display_number" not in create.await_args.kwargs


@pytest.mark.asyncio
async def test_standalone_browser_launch_error_stops_vnc_and_marks_failed(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))

    with (
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(
            RealBrowserManager,
            "_create_browser_state",
            new=AsyncMock(side_effect=RuntimeError("browser launch failed")),
        ),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    stop_vnc.assert_awaited_once_with("pbs_1", organization_id="org_1")
    manager.update_status.assert_awaited_once_with("pbs_1", "org_1", PersistentBrowserSessionStatus.failed)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["page", "status", "started_at"])
async def test_standalone_launch_stage_failure_closes_browser_before_vnc(
    manager: DefaultPersistentSessionsManager,
    failure_stage: str,
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    browser_state = MagicMock()
    browser_state.get_or_create_page = AsyncMock(
        side_effect=RuntimeError("page failed") if failure_stage == "page" else None
    )
    close_order: list[str] = []

    async def close_browser() -> None:
        close_order.append("browser")

    async def update_status_side_effect(
        _session_id: str,
        _organization_id: str,
        status: PersistentBrowserSessionStatus,
    ) -> SimpleNamespace:
        if failure_stage == "status" and status == PersistentBrowserSessionStatus.running:
            raise RuntimeError("status failed")
        return session

    async def stop_vnc(*_args: object, **_kwargs: object) -> None:
        close_order.append("vnc")

    browser_state.close = AsyncMock(side_effect=close_browser)
    manager.update_status = AsyncMock(side_effect=update_status_side_effect)
    if failure_stage == "started_at":
        manager.database.browser_sessions.update_persistent_browser_session.side_effect = RuntimeError(
            "started_at failed"
        )

    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))
    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(return_value=browser_state)),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock(side_effect=stop_vnc)),
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    browser_state.close.assert_awaited_once()
    assert close_order == ["browser", "vnc"]


@pytest.mark.asyncio
async def test_standalone_launch_cancellation_closes_browser_and_vnc_before_propagating(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    page_started = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    browser_state = MagicMock()

    async def block_page(**_kwargs: object) -> None:
        page_started.set()
        await asyncio.Event().wait()

    async def close_browser() -> None:
        close_started.set()
        await release_close.wait()

    browser_state.get_or_create_page = AsyncMock(side_effect=block_page)
    browser_state.close = AsyncMock(side_effect=close_browser)
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(return_value=browser_state)),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        task = asyncio.create_task(manager._launch_browser_for_session("pbs_1", "org_1"))
        await page_started.wait()
        task.cancel()
        await close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    browser_state.close.assert_awaited_once()
    stop_vnc.assert_awaited_once_with("pbs_1", organization_id="org_1")


@pytest.mark.asyncio
async def test_duplicate_browser_close_failure_does_not_finalize_winning_vnc_session(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    winner = MagicMock()
    loser = MagicMock()
    loser.get_or_create_page = AsyncMock()
    loser.close = AsyncMock(side_effect=RuntimeError("loser close failed"))
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))

    async def create_loser(**_kwargs: object) -> MagicMock:
        manager._browser_sessions["pbs_1"] = BrowserSession(browser_state=winner, organization_id="org_1")
        return loser

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(side_effect=create_loser)),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    loser.close.assert_awaited_once()
    assert manager._browser_sessions["pbs_1"].browser_state is winner
    stop_vnc.assert_not_awaited()
    manager.update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_standalone_launches_create_one_browser_and_one_running_transition(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    create_started = asyncio.Event()
    release_create = asyncio.Event()
    browser_state = MagicMock()
    browser_state.get_or_create_page = AsyncMock()
    browser_state.close = AsyncMock()

    async def create_browser(**_kwargs: object) -> MagicMock:
        create_started.set()
        await release_create.wait()
        return browser_state

    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))
    create = AsyncMock(side_effect=create_browser)
    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=create),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        first = asyncio.create_task(manager._launch_browser_for_session("pbs_1", "org_1"))
        await create_started.wait()
        second = asyncio.create_task(manager._launch_browser_for_session("pbs_1", "org_1"))
        await asyncio.sleep(0)
        release_create.set()
        await asyncio.gather(first, second)

    create.assert_awaited_once()
    manager.update_status.assert_awaited_once_with("pbs_1", "org_1", PersistentBrowserSessionStatus.running)
    manager.database.browser_sessions.update_persistent_browser_session.assert_awaited_once()
    browser_state.close.assert_not_awaited()
    stop_vnc.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_registration_waits_for_launch_winner_and_closes_rejected_candidate(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    create_started = asyncio.Event()
    release_create = asyncio.Event()
    launch_winner = MagicMock()
    launch_winner.get_or_create_page = AsyncMock()
    launch_winner.close = AsyncMock()
    external_candidate = MagicMock()
    external_candidate.close = AsyncMock()

    async def create_browser(**_kwargs: object) -> MagicMock:
        create_started.set()
        await release_create.wait()
        return launch_winner

    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))
    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(side_effect=create_browser)),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        launch = asyncio.create_task(manager._launch_browser_for_session("pbs_1", "org_1"))
        await create_started.wait()
        registration = asyncio.create_task(
            manager.set_browser_state("pbs_1", external_candidate, organization_id="org_1")
        )
        release_create.set()
        await asyncio.gather(launch, registration)

    assert manager._browser_sessions["pbs_1"].browser_state is launch_winner
    external_candidate.close.assert_awaited_once()
    launch_winner.close.assert_not_awaited()
    stop_vnc.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_closed_status_transition_closes_and_removes_cached_browser(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=None, vnc_port=None)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=None)
    browser_state = MagicMock()
    browser_state.get_or_create_page = AsyncMock()
    browser_state.close = AsyncMock()
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "cdp"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(return_value=browser_state)),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    browser_state.close.assert_awaited_once()
    assert "pbs_1" not in manager._browser_sessions
    stop_vnc.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_standalone_launch_does_not_use_vnc_session_lock(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=None, vnc_port=None)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    browser_state = MagicMock()
    browser_state.get_or_create_page = AsyncMock()
    browser_state.close = AsyncMock()
    manager._get_session_lock = MagicMock(side_effect=AssertionError("VNC-only lock used for CDP"))
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "cdp"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(return_value=browser_state)),
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    assert manager._browser_sessions["pbs_1"].browser_state is browser_state


@pytest.mark.asyncio
async def test_cdp_duplicate_close_error_preserves_existing_winner_and_legacy_swallow_behavior(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = session_row(display_number=None, vnc_port=None)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock()
    winner = MagicMock()
    loser = MagicMock()
    loser.get_or_create_page = AsyncMock()
    loser.close = AsyncMock(side_effect=RuntimeError("duplicate close failed"))

    async def create_loser(**_kwargs: object) -> MagicMock:
        manager._browser_sessions["pbs_1"] = BrowserSession(browser_state=winner, organization_id="org_1")
        return loser

    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))
    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "cdp"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(side_effect=create_loser)),
    ):
        await manager._launch_browser_for_session("pbs_1", "org_1")

    loser.close.assert_awaited_once()
    assert manager._browser_sessions["pbs_1"].browser_state is winner
    manager.update_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("control_flow", [SystemExit, KeyboardInterrupt])
async def test_standalone_launch_preserves_non_exception_control_flow_after_cleanup(
    manager: DefaultPersistentSessionsManager,
    control_flow: type[BaseException],
) -> None:
    session = session_row(display_number=104, vnc_port=6084)
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))

    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_module, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(side_effect=control_flow("stop"))),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        with pytest.raises(control_flow):
            await manager._launch_browser_for_session("pbs_1", "org_1")

    stop_vnc.assert_awaited_once_with("pbs_1", organization_id="org_1")
    manager.update_status.assert_awaited_once_with("pbs_1", "org_1", PersistentBrowserSessionStatus.failed)


@pytest.mark.asyncio
async def test_manager_shutdown_stops_all_vnc_even_when_database_lookup_fails(
    manager: DefaultPersistentSessionsManager,
) -> None:
    manager.database.browser_sessions.get_all_active_persistent_browser_sessions.side_effect = RuntimeError(
        "database unavailable"
    )
    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_all", new=AsyncMock()) as stop_all,
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            await DefaultPersistentSessionsManager.close()

    stop_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_shutdown_does_not_touch_vnc_manager_in_cdp_mode(
    manager: DefaultPersistentSessionsManager,
) -> None:
    with (
        patch.object(manager_module.settings, "BROWSER_STREAMING_MODE", "cdp"),
        patch.object(VncManager, "stop_all", new=AsyncMock()) as stop_all,
    ):
        await DefaultPersistentSessionsManager.close()

    stop_all.assert_not_awaited()
