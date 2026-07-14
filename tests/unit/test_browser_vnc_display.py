from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye import browser_factory, real_browser_manager
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_factory import (
    BrowserContextFactory,
    _create_headful_chromium,
    _create_headless_chromium,
)
from skyvern.webeye.real_browser_manager import RealBrowserManager
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.vnc_manager import VncStartupError


def patch_local_chromium_launch(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    launch = AsyncMock(return_value=MagicMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(browser_factory, "make_temp_directory", lambda **_kwargs: "/tmp/browser-profile")
    monkeypatch.setattr(browser_factory, "initialize_download_dir", lambda: "/tmp/downloads")
    monkeypatch.setattr(BrowserContextFactory, "update_chromium_browser_preferences", MagicMock())
    monkeypatch.setattr(
        BrowserContextFactory,
        "build_browser_args",
        MagicMock(return_value={"record_har_path": None}),
    )
    return playwright


@pytest.mark.asyncio
@pytest.mark.parametrize("creator", [_create_headful_chromium, _create_headless_chromium])
async def test_local_chromium_receives_session_display_in_child_environment(
    creator: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playwright = patch_local_chromium_launch(monkeypatch)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("SKYVERN_TEST_PARENT_ENV", "preserved")

    await creator(playwright, display_number=107)  # type: ignore[operator]

    launch_kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
    assert launch_kwargs["env"]["DISPLAY"] == ":107"
    assert launch_kwargs["env"]["SKYVERN_TEST_PARENT_ENV"] == "preserved"
    assert "DISPLAY" not in os.environ


@pytest.mark.asyncio
@pytest.mark.parametrize("creator", [_create_headful_chromium, _create_headless_chromium])
async def test_local_chromium_omits_child_environment_without_session_display(
    creator: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playwright = patch_local_chromium_launch(monkeypatch)

    await creator(playwright)  # type: ignore[operator]

    assert "env" not in playwright.chromium.launch_persistent_context.await_args.kwargs


@pytest.mark.asyncio
async def test_create_browser_state_threads_display_to_context_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    pw = MagicMock()
    launcher = MagicMock()
    launcher.return_value.start = AsyncMock(return_value=pw)
    monkeypatch.setattr(real_browser_manager, "async_playwright", launcher)
    create_context = AsyncMock(return_value=(MagicMock(), BrowserArtifacts(), None))
    monkeypatch.setattr(BrowserContextFactory, "create_browser_context", create_context)

    await RealBrowserManager._create_browser_state(display_number=108)

    assert create_context.await_args.kwargs["display_number"] == 108


@pytest.mark.asyncio
async def test_create_browser_state_omits_display_kwarg_when_not_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pw = MagicMock()
    launcher = MagicMock()
    launcher.return_value.start = AsyncMock(return_value=pw)
    monkeypatch.setattr(real_browser_manager, "async_playwright", launcher)
    create_context = AsyncMock(return_value=(MagicMock(), BrowserArtifacts(), None))
    monkeypatch.setattr(BrowserContextFactory, "create_browser_context", create_context)

    await RealBrowserManager._create_browser_state()

    assert "display_number" not in create_context.await_args.kwargs


def fake_browser_state() -> MagicMock:
    state = MagicMock()
    state.get_or_create_page = AsyncMock()
    state.close = AsyncMock()
    state.browser_artifacts = BrowserArtifacts()
    return state


@pytest.mark.asyncio
async def test_task_session_display_reaches_browser_state_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    task = SimpleNamespace(
        task_id="tsk_1",
        organization_id="org_1",
        proxy_location=None,
        workflow_run_id=None,
        url="https://example.com",
        workflow_permanent_id=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
    )
    session = SimpleNamespace(proxy_location=None, proxy_session_id=None, display_number=109)
    candidate = fake_browser_state()
    winner = fake_browser_state()
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(return_value=session),
        set_browser_state=AsyncMock(),
        compare_and_install_browser_state=AsyncMock(return_value=winner),
        requires_local_vnc_display=MagicMock(return_value=True),
    )
    monkeypatch.setattr(
        real_browser_manager,
        "app",
        SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager),
    )
    create = AsyncMock(return_value=candidate)
    monkeypatch.setattr(manager, "_create_browser_state", create)

    result = await manager.get_or_create_for_task(task, browser_session_id="pbs_1")

    assert create.await_args.kwargs["display_number"] == 109
    assert result is winner
    assert manager.pages["tsk_1"] is winner
    persistent_manager.compare_and_install_browser_state.assert_awaited_once_with("pbs_1", candidate, "org_1")
    persistent_manager.set_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_session_display_reaches_browser_state_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_1",
        parent_workflow_run_id=None,
        browser_profile_id=None,
        proxy_location=None,
        organization_id="org_1",
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
        workflow_permanent_id=None,
    )
    session = SimpleNamespace(proxy_location=None, proxy_session_id=None, display_number=110)
    candidate = fake_browser_state()
    winner = fake_browser_state()
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(return_value=session),
        set_browser_state=AsyncMock(),
        compare_and_install_browser_state=AsyncMock(return_value=winner),
        requires_local_vnc_display=MagicMock(return_value=True),
    )
    monkeypatch.setattr(
        real_browser_manager,
        "app",
        SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager),
    )
    create = AsyncMock(return_value=candidate)
    monkeypatch.setattr(manager, "_create_browser_state", create)

    result = await manager.get_or_create_for_workflow_run(
        workflow_run,
        url="https://example.com",
        browser_session_id="pbs_1",
    )

    assert create.await_args.kwargs["display_number"] == 110
    assert result is winner
    assert manager.pages["wr_1"] is winner
    persistent_manager.compare_and_install_browser_state.assert_awaited_once_with("pbs_1", candidate, "org_1")
    persistent_manager.set_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_task_preserves_browser_creation_call_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    task = SimpleNamespace(
        task_id="tsk_cdp",
        organization_id="org_1",
        proxy_location=None,
        workflow_run_id=None,
        url="https://example.com",
        workflow_permanent_id=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
    )
    session = SimpleNamespace(proxy_location=None, proxy_session_id=None, display_number=109)
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(return_value=session),
        set_browser_state=AsyncMock(),
    )
    monkeypatch.setattr(
        real_browser_manager,
        "app",
        SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager),
    )
    create = AsyncMock(return_value=fake_browser_state())
    monkeypatch.setattr(manager, "_create_browser_state", create)

    await manager.get_or_create_for_task(task, browser_session_id="pbs_1")

    assert "display_number" not in create.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session", [None, SimpleNamespace(proxy_location=None, proxy_session_id=None, display_number=None)]
)
async def test_task_local_vnc_fails_closed_without_session_display(
    session: SimpleNamespace | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RealBrowserManager()
    task = SimpleNamespace(
        task_id="tsk_missing_display",
        organization_id="org_1",
        proxy_location=None,
        workflow_run_id=None,
        url="https://example.com",
        workflow_permanent_id=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
    )
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(return_value=session),
        set_browser_state=AsyncMock(),
        requires_local_vnc_display=MagicMock(return_value=True),
    )
    monkeypatch.setattr(real_browser_manager, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager))
    create = AsyncMock(return_value=fake_browser_state())
    monkeypatch.setattr(manager, "_create_browser_state", create)

    with pytest.raises(VncStartupError, match="display"):
        await manager.get_or_create_for_task(task, browser_session_id="pbs_1")

    create.assert_not_awaited()
    persistent_manager.set_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_local_vnc_fails_closed_without_session_display(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_missing_display",
        parent_workflow_run_id=None,
        browser_profile_id=None,
        proxy_location=None,
        organization_id="org_1",
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
        workflow_permanent_id=None,
    )
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(return_value=None),
        set_browser_state=AsyncMock(),
        requires_local_vnc_display=MagicMock(return_value=True),
    )
    monkeypatch.setattr(real_browser_manager, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager))
    create = AsyncMock(return_value=fake_browser_state())
    monkeypatch.setattr(manager, "_create_browser_state", create)

    with pytest.raises(VncStartupError, match="display"):
        await manager.get_or_create_for_workflow_run(
            workflow_run,
            url="https://example.com",
            browser_session_id="pbs_1",
        )

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_script_cache_miss_threads_org_and_display(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    session = SimpleNamespace(proxy_location=None, proxy_session_id=None, display_number=112)
    candidate = fake_browser_state()
    winner = fake_browser_state()
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(return_value=session),
        set_browser_state=AsyncMock(),
        compare_and_install_browser_state=AsyncMock(return_value=winner),
        requires_local_vnc_display=MagicMock(return_value=True),
    )
    monkeypatch.setattr(real_browser_manager, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager))
    monkeypatch.setattr(
        real_browser_manager.skyvern_context,
        "current",
        MagicMock(return_value=SkyvernContext(organization_id="org_1")),
    )
    create = AsyncMock(return_value=candidate)
    monkeypatch.setattr(manager, "_create_browser_state", create)

    result = await manager.get_or_create_for_script(
        script_id="script_1",
        browser_session_id="pbs_1",
    )

    persistent_manager.get_browser_state.assert_awaited_once_with("pbs_1", organization_id="org_1")
    persistent_manager.get_session.assert_awaited_once_with("pbs_1", "org_1")
    assert create.await_args.kwargs["display_number"] == 112
    assert result is winner
    assert manager.pages["script_1"] is winner
    persistent_manager.compare_and_install_browser_state.assert_awaited_once_with("pbs_1", candidate, "org_1")
    persistent_manager.set_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_script_local_vnc_fails_closed_without_display(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(return_value=None),
        set_browser_state=AsyncMock(),
        requires_local_vnc_display=MagicMock(return_value=True),
    )
    monkeypatch.setattr(real_browser_manager, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager))
    monkeypatch.setattr(
        real_browser_manager.skyvern_context,
        "current",
        MagicMock(return_value=SkyvernContext(organization_id="org_1")),
    )
    create = AsyncMock(return_value=fake_browser_state())
    monkeypatch.setattr(manager, "_create_browser_state", create)

    with pytest.raises(VncStartupError, match="display"):
        await manager.get_or_create_for_script(browser_session_id="pbs_1")

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_script_preserves_cache_and_browser_call_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    browser_state = fake_browser_state()
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(),
        set_browser_state=AsyncMock(),
        requires_local_vnc_display=MagicMock(return_value=False),
    )
    monkeypatch.setattr(real_browser_manager, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager))
    create = AsyncMock(return_value=browser_state)
    monkeypatch.setattr(manager, "_create_browser_state", create)

    await manager.get_or_create_for_script(
        script_id="script_cdp",
        browser_session_id="pbs_cdp",
    )

    persistent_manager.get_browser_state.assert_awaited_once_with("pbs_cdp", organization_id="script_cdp")
    persistent_manager.get_session.assert_not_awaited()
    persistent_manager.set_browser_state.assert_not_awaited()
    assert create.await_args.kwargs == {
        "proxy_location": real_browser_manager.ProxyLocation.RESIDENTIAL,
        "script_id": "script_cdp",
    }
    assert browser_state.get_or_create_page.await_args.kwargs == {
        "proxy_location": real_browser_manager.ProxyLocation.RESIDENTIAL,
        "script_id": "script_cdp",
    }


@pytest.mark.asyncio
async def test_cloud_manager_does_not_require_local_display_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    task = SimpleNamespace(
        task_id="tsk_cloud",
        organization_id="org_1",
        proxy_location=None,
        workflow_run_id=None,
        url="https://example.com",
        workflow_permanent_id=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
    )
    persistent_manager = SimpleNamespace(
        get_browser_state=AsyncMock(return_value=None),
        get_session=AsyncMock(return_value=None),
        set_browser_state=AsyncMock(),
    )
    monkeypatch.setattr(real_browser_manager, "app", SimpleNamespace(PERSISTENT_SESSIONS_MANAGER=persistent_manager))
    create = AsyncMock(return_value=fake_browser_state())
    monkeypatch.setattr(manager, "_create_browser_state", create)

    await manager.get_or_create_for_task(task, browser_session_id="pbs_cloud")

    assert "display_number" not in create.await_args.kwargs


@pytest.mark.asyncio
async def test_real_browser_state_recreation_reuses_assigned_display(monkeypatch: pytest.MonkeyPatch) -> None:
    context = MagicMock()
    page = MagicMock()
    create_context = AsyncMock(return_value=(context, BrowserArtifacts(), None))
    monkeypatch.setattr(BrowserContextFactory, "create_browser_context", create_context)
    state = RealBrowserState(pw=MagicMock(), display_number=113)
    monkeypatch.setattr(state, "get_working_page", AsyncMock(return_value=page))

    await state.check_and_fix_state()

    assert create_context.await_args.kwargs["display_number"] == 113


@pytest.mark.asyncio
async def test_real_browser_state_cdp_recreation_omits_display_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    context = MagicMock()
    page = MagicMock()
    create_context = AsyncMock(return_value=(context, BrowserArtifacts(), None))
    monkeypatch.setattr(BrowserContextFactory, "create_browser_context", create_context)
    state = RealBrowserState(pw=MagicMock())
    monkeypatch.setattr(state, "get_working_page", AsyncMock(return_value=page))

    await state.check_and_fix_state()

    assert "display_number" not in create_context.await_args.kwargs


@pytest.mark.asyncio
async def test_context_factory_failure_stops_playwright_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    pw = MagicMock()
    pw.stop = AsyncMock()
    launcher = MagicMock()
    launcher.return_value.start = AsyncMock(return_value=pw)
    monkeypatch.setattr(real_browser_manager, "async_playwright", launcher)
    monkeypatch.setattr(
        BrowserContextFactory,
        "create_browser_context",
        AsyncMock(side_effect=RuntimeError("context failed")),
    )

    with pytest.raises(RuntimeError, match="context failed"):
        await RealBrowserManager._create_browser_state(display_number=114)

    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_creation_cancellation_repeatedly_cancelled_still_stops_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_started = asyncio.Event()
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    pw = MagicMock()

    async def create_context(*_args: object, **_kwargs: object) -> object:
        factory_started.set()
        await asyncio.Event().wait()

    async def stop_driver() -> None:
        stop_started.set()
        await release_stop.wait()

    pw.stop = AsyncMock(side_effect=stop_driver)
    launcher = MagicMock()
    launcher.return_value.start = AsyncMock(return_value=pw)
    monkeypatch.setattr(real_browser_manager, "async_playwright", launcher)
    monkeypatch.setattr(BrowserContextFactory, "create_browser_context", create_context)

    task = asyncio.create_task(RealBrowserManager._create_browser_state(display_number=115))
    await factory_started.wait()
    task.cancel()
    await asyncio.wait_for(stop_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_stop.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    pw.stop.assert_awaited_once()
