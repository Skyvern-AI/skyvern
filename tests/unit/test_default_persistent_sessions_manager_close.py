import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import BrowserSessionClosed
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSessionStatus
from skyvern.webeye import default_persistent_sessions_manager as manager_mod
from skyvern.webeye.default_persistent_sessions_manager import BrowserSession, DefaultPersistentSessionsManager
from skyvern.webeye.real_browser_manager import RealBrowserManager
from skyvern.webeye.vnc_manager import VncManager, VncTeardownError


@pytest.fixture
def manager() -> DefaultPersistentSessionsManager:
    DefaultPersistentSessionsManager.instance = None
    DefaultPersistentSessionsManager._browser_sessions = {}
    DefaultPersistentSessionsManager._background_tasks = set()
    DefaultPersistentSessionsManager._reaper_task = None
    DefaultPersistentSessionsManager._session_locks = {}
    db = MagicMock()
    db.browser_sessions.get_persistent_browser_session = AsyncMock()
    db.browser_sessions.close_persistent_browser_session = AsyncMock()
    db.browser_sessions.archive_browser_session_address = AsyncMock()
    return DefaultPersistentSessionsManager(database=db)


@pytest.mark.asyncio
async def test_close_session_skips_in_memory_export_for_mismatched_org(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_artifacts = SimpleNamespace(
        browser_session_dir="/tmp/pbs_foreign",
        video_artifacts=[],
    )
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()

    manager._browser_sessions["pbs_foreign"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()) as persist_session_cookies,
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        await manager.close_session("org_requester", "pbs_foreign")

    persist_session_cookies.assert_not_awaited()
    storage.store_browser_profile.assert_not_awaited()
    browser_state.close.assert_not_awaited()
    stop_vnc.assert_not_awaited()
    assert "pbs_foreign" in manager._browser_sessions
    manager.database.browser_sessions.get_persistent_browser_session.assert_not_awaited()
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_foreign",
        "org_requester",
    )


@pytest.mark.asyncio
async def test_get_browser_state_rejects_cross_org_cached_session(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    manager._browser_sessions["pbs_foreign"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"):
        result = await manager.get_browser_state("pbs_foreign", organization_id="org_requester")

    assert result is None
    assert manager._browser_sessions["pbs_foreign"].browser_state is browser_state


@pytest.mark.asyncio
async def test_cdp_get_browser_state_preserves_cross_org_cache_lookup_behavior(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    manager._browser_sessions["pbs_cdp"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "cdp"):
        result = await manager.get_browser_state("pbs_cdp", organization_id="org_requester")

    assert result is browser_state


@pytest.mark.asyncio
async def test_close_session_exports_and_closes_for_matching_org(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_context = MagicMock()
    browser_state.browser_artifacts = SimpleNamespace(
        browser_session_dir="/tmp/pbs_owned",
        video_artifacts=[],
    )
    storage = MagicMock()
    storage.store_browser_profile = AsyncMock()
    persisted_session = MagicMock()
    persisted_session.should_export_profile.return_value = True
    manager.database.browser_sessions.get_persistent_browser_session.return_value = persisted_session

    manager._browser_sessions["pbs_owned"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    close_order: list[str] = []

    async def close_browser() -> None:
        close_order.append("browser")

    async def stop_vnc(*_args: object, **_kwargs: object) -> None:
        close_order.append("vnc")

    browser_state.close.side_effect = close_browser

    with (
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(manager_mod, "persist_session_cookies", new=AsyncMock()) as persist_session_cookies,
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock(side_effect=stop_vnc)) as stop_vnc_mock,
    ):
        await manager.close_session("org_owner", "pbs_owned")

    persist_session_cookies.assert_awaited_once_with(browser_state.browser_context, "/tmp/pbs_owned")
    manager.database.browser_sessions.get_persistent_browser_session.assert_awaited_once_with(
        "pbs_owned",
        "org_owner",
    )
    storage.store_browser_profile.assert_awaited_once_with(
        organization_id="org_owner",
        profile_id="pbs_owned",
        directory="/tmp/pbs_owned",
    )
    browser_state.close.assert_awaited_once()
    stop_vnc_mock.assert_awaited_once_with("pbs_owned", organization_id="org_owner")
    assert close_order == ["browser", "vnc"]
    assert "pbs_owned" not in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_owned",
        "org_owner",
    )


@pytest.mark.asyncio
async def test_close_session_stops_vnc_when_browser_close_raises(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock(side_effect=RuntimeError("close failed"))
    browser_state.browser_artifacts = None
    manager._browser_sessions["pbs_owned"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        await manager.close_session("org_owner", "pbs_owned")

    browser_state.close.assert_awaited_once()
    stop_vnc.assert_awaited_once_with("pbs_owned", organization_id="org_owner")
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_owned", "org_owner"
    )


@pytest.mark.asyncio
async def test_close_session_stops_vnc_even_without_cached_browser(
    manager: DefaultPersistentSessionsManager,
) -> None:
    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        await manager.close_session("org_owner", "pbs_vnc_only")

    stop_vnc.assert_awaited_once_with("pbs_vnc_only", organization_id="org_owner")


@pytest.mark.asyncio
async def test_close_session_does_not_finalize_when_vnc_ownership_check_fails(
    manager: DefaultPersistentSessionsManager,
) -> None:
    mismatch = VncTeardownError(
        "pbs_foreign",
        survivors=("Xvfb",),
        errors=("organization does not own this VNC stack",),
    )
    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock(side_effect=mismatch)),
    ):
        with pytest.raises(VncTeardownError, match="remains tracked"):
            await manager.close_session("org_requester", "pbs_foreign")

    manager.database.browser_sessions.close_persistent_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_cookie_persistence_error_preserves_prior_close_behavior(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_context = MagicMock()
    browser_state.browser_artifacts = SimpleNamespace(
        browser_session_dir="/tmp/pbs_cdp",
        video_artifacts=[],
    )
    manager._browser_sessions["pbs_cdp"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "cdp"),
        patch.object(
            manager_mod,
            "persist_session_cookies",
            new=AsyncMock(side_effect=RuntimeError("cookie write failed")),
        ),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        with pytest.raises(RuntimeError, match="cookie write failed"):
            await manager.close_session("org_owner", "pbs_cdp")

    browser_state.close.assert_not_awaited()
    stop_vnc.assert_not_awaited()
    assert "pbs_cdp" in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_stops_vnc_before_video_sync(
    manager: DefaultPersistentSessionsManager,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "2026-07-13" / "recording.webm"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    close_order: list[str] = []
    browser_state = MagicMock()

    async def close_browser() -> None:
        close_order.append("browser")

    async def stop_vnc(*_args: object, **_kwargs: object) -> None:
        close_order.append("vnc")

    async def sync_video(**_kwargs: object) -> None:
        close_order.append("video")

    browser_state.close = AsyncMock(side_effect=close_browser)
    browser_state.browser_artifacts = SimpleNamespace(
        browser_session_dir=None,
        video_artifacts=[SimpleNamespace(video_path=str(video_path))],
    )
    manager._browser_sessions["pbs_owned"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )
    storage = SimpleNamespace(sync_browser_session_file=AsyncMock(side_effect=sync_video))

    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock(side_effect=stop_vnc)),
    ):
        await manager.close_session("org_owner", "pbs_owned")

    assert close_order == ["browser", "vnc", "video"]


@pytest.mark.asyncio
async def test_close_retains_cache_and_skips_video_when_vnc_stop_needs_retry(
    manager: DefaultPersistentSessionsManager,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "2026-07-13" / "recording.webm"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    browser_state = MagicMock()
    browser_state.close = AsyncMock()
    browser_state.browser_artifacts = SimpleNamespace(
        browser_session_dir=None,
        video_artifacts=[SimpleNamespace(video_path=str(video_path))],
    )
    manager._browser_sessions["pbs_retry"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )
    storage = SimpleNamespace(sync_browser_session_file=AsyncMock())
    teardown_error = VncTeardownError("pbs_retry", survivors=("Xvfb",))

    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_mod, "app", SimpleNamespace(STORAGE=storage)),
        patch.object(
            VncManager,
            "stop_vnc_for_session",
            new=AsyncMock(side_effect=[teardown_error, None]),
        ),
    ):
        with pytest.raises(VncTeardownError, match="remains tracked"):
            await manager.close_session("org_owner", "pbs_retry")

    storage.sync_browser_session_file.assert_not_awaited()
    assert "pbs_retry" in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_session_repeated_cancellation_waits_for_browser_then_vnc_and_propagates(
    manager: DefaultPersistentSessionsManager,
) -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    browser_state = MagicMock()

    async def close_browser() -> None:
        close_started.set()
        await release_close.wait()

    browser_state.close = AsyncMock(side_effect=close_browser)
    browser_state.browser_artifacts = None
    manager._browser_sessions["pbs_owned"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )

    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        task = asyncio.create_task(manager.close_session("org_owner", "pbs_owned"))
        await close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert "pbs_owned" in manager._browser_sessions
        release_close.set()

        with pytest.raises(asyncio.CancelledError):
            await task

    browser_state.close.assert_awaited_once()
    stop_vnc.assert_awaited_once_with("pbs_owned", organization_id="org_owner")
    assert "pbs_owned" not in manager._browser_sessions
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with(
        "pbs_owned",
        "org_owner",
    )


def active_session_row(*, completed: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        persistent_browser_session_id="pbs_race",
        organization_id="org_owner",
        status=(PersistentBrowserSessionStatus.completed if completed else PersistentBrowserSessionStatus.created),
        completed_at=object() if completed else None,
        proxy_location=None,
        proxy_session_id=None,
        browser_profile_id=None,
        display_number=104,
        vnc_port=6084,
    )


@pytest.mark.asyncio
async def test_close_waits_for_inflight_launch_then_closes_browser_before_vnc(
    manager: DefaultPersistentSessionsManager,
) -> None:
    session = active_session_row()
    manager.get_session = AsyncMock(return_value=session)
    manager.update_status = AsyncMock(return_value=session)
    manager.database.browser_sessions.update_persistent_browser_session = AsyncMock(return_value=session)
    create_started = asyncio.Event()
    release_create = asyncio.Event()
    order: list[str] = []
    browser_state = MagicMock()
    browser_state.browser_artifacts = None
    browser_state.get_or_create_page = AsyncMock()

    async def create_browser(**_kwargs: object) -> MagicMock:
        create_started.set()
        await release_create.wait()
        return browser_state

    async def close_browser() -> None:
        order.append("browser")

    async def stop_vnc(*_args: object, **_kwargs: object) -> None:
        order.append("vnc")

    browser_state.close = AsyncMock(side_effect=close_browser)
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))
    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_mod, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=AsyncMock(side_effect=create_browser)),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock(side_effect=stop_vnc)),
    ):
        launch = asyncio.create_task(manager._launch_browser_for_session("pbs_race", "org_owner"))
        await create_started.wait()
        close = asyncio.create_task(manager.close_session("org_owner", "pbs_race"))
        await asyncio.sleep(0)
        release_create.set()
        await asyncio.gather(launch, close)

    assert order == ["browser", "vnc"]
    assert "pbs_race" not in manager._browser_sessions


@pytest.mark.asyncio
async def test_launch_waits_for_close_and_does_not_create_browser_after_finalization(
    manager: DefaultPersistentSessionsManager,
) -> None:
    close_db_started = asyncio.Event()
    release_close_db = asyncio.Event()
    closed = False
    active = active_session_row()
    final = active_session_row(completed=True)

    async def close_db(*_args: object, **_kwargs: object) -> None:
        nonlocal closed
        close_db_started.set()
        await release_close_db.wait()
        closed = True

    async def get_session(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return final if closed else active

    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock(side_effect=close_db)
    manager.get_session = AsyncMock(side_effect=get_session)
    create = AsyncMock()
    agent_function = SimpleNamespace(build_proxy_session_extra_http_headers=MagicMock(return_value=None))
    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(manager_mod, "app", SimpleNamespace(AGENT_FUNCTION=agent_function)),
        patch.object(RealBrowserManager, "_create_browser_state", new=create),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()),
    ):
        close = asyncio.create_task(manager.close_session("org_owner", "pbs_race"))
        await close_db_started.wait()
        launch = asyncio.create_task(manager._launch_browser_for_session("pbs_race", "org_owner"))
        await asyncio.sleep(0)
        release_close_db.set()
        await asyncio.gather(close, launch)

    create.assert_not_awaited()
    assert "pbs_race" not in manager._browser_sessions


@pytest.mark.asyncio
async def test_external_registration_waits_for_close_and_rejects_candidate_after_finalization(
    manager: DefaultPersistentSessionsManager,
) -> None:
    active = active_session_row()
    final = active_session_row(completed=True)
    browser_close_started = asyncio.Event()
    release_browser_close = asyncio.Event()
    closed = False
    incumbent = MagicMock()
    incumbent.browser_artifacts = None
    candidate = MagicMock()
    candidate.close = AsyncMock()

    async def close_browser() -> None:
        browser_close_started.set()
        await release_browser_close.wait()

    async def close_db(*_args: object, **_kwargs: object) -> None:
        nonlocal closed
        closed = True

    async def get_session(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return final if closed else active

    incumbent.close = AsyncMock(side_effect=close_browser)
    manager._browser_sessions["pbs_race"] = BrowserSession(
        browser_state=incumbent,
        organization_id="org_owner",
    )
    manager.get_session = AsyncMock(side_effect=get_session)
    manager.database.browser_sessions.close_persistent_browser_session = AsyncMock(side_effect=close_db)

    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "vnc"),
        patch.object(VncManager, "stop_vnc_for_session", new=AsyncMock()) as stop_vnc,
    ):
        close = asyncio.create_task(manager.close_session("org_owner", "pbs_race"))
        await browser_close_started.wait()
        registration = asyncio.create_task(
            manager.set_browser_state("pbs_race", candidate, organization_id="org_owner")
        )
        await asyncio.sleep(0)
        release_browser_close.set()
        await close
        with pytest.raises(BrowserSessionClosed):
            await registration

    candidate.close.assert_awaited_once()
    assert "pbs_race" not in manager._browser_sessions
    stop_vnc.assert_awaited_once_with("pbs_race", organization_id="org_owner")


@pytest.mark.asyncio
async def test_cdp_close_cancellation_preserves_cache_and_port_ownership(
    manager: DefaultPersistentSessionsManager,
) -> None:
    close_started = asyncio.Event()
    browser_state = MagicMock()
    browser_state.browser_artifacts = None

    async def close_browser() -> None:
        close_started.set()
        await asyncio.Event().wait()

    browser_state.close = AsyncMock(side_effect=close_browser)
    manager._browser_sessions["pbs_cdp"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
        cdp_port=9223,
    )

    with (
        patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "cdp"),
        patch.object(manager_mod, "_release_cdp_port") as release_cdp_port,
    ):
        close = asyncio.create_task(manager.close_session("org_owner", "pbs_cdp"))
        await close_started.wait()
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close

    assert manager._browser_sessions["pbs_cdp"].browser_state is browser_state
    release_cdp_port.assert_not_called()
    manager.database.browser_sessions.close_persistent_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_close_does_not_use_vnc_session_lock_or_terminal_barrier(
    manager: DefaultPersistentSessionsManager,
) -> None:
    browser_state = MagicMock()
    browser_state.browser_artifacts = None
    browser_state.close = AsyncMock()
    manager._browser_sessions["pbs_cdp"] = BrowserSession(
        browser_state=browser_state,
        organization_id="org_owner",
    )
    manager._get_session_lock = MagicMock(side_effect=AssertionError("VNC-only lock used for CDP"))

    with patch.object(manager_mod.settings, "BROWSER_STREAMING_MODE", "cdp"):
        await manager.close_session("org_owner", "pbs_cdp")

    browser_state.close.assert_awaited_once()
    manager.database.browser_sessions.close_persistent_browser_session.assert_awaited_once_with("pbs_cdp", "org_owner")
