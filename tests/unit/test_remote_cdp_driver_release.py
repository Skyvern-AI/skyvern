"""Local Playwright driver release for remote-CDP browser states.

A ``BrowserState`` created for a caller-provided remote browser
(``browser_address``) is closed with ``close_browser_on_completion=False`` so
the remote browser survives the run — but the per-run local Playwright driver
(a Node subprocess) must still be released, otherwise every such run leaks a
driver process until the service is OOM-killed.

The reuse invariant must hold: states retained for reuse (persistent sessions,
browsers shared across parent/child runs) must NOT have their driver stopped.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.real_browser_manager import RealBrowserManager
from skyvern.webeye.real_browser_state import RealBrowserState


def _pw_stub() -> MagicMock:
    pw = MagicMock()
    pw.stop = AsyncMock()
    return pw


def _context_stub() -> MagicMock:
    context = MagicMock()
    context.close = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_close_stops_driver_for_remote_cdp_state_and_keeps_remote_browser() -> None:
    pw = _pw_stub()
    context = _context_stub()
    state = RealBrowserState(pw=pw, browser_context=context, release_driver_on_close=True)

    await state.close(close_browser_on_completion=False)

    pw.stop.assert_awaited_once()
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_keeps_driver_by_default_when_browser_kept() -> None:
    """Persistent-session / shared states rely on close(False) leaving the driver alive."""
    pw = _pw_stub()
    context = _context_stub()
    state = RealBrowserState(pw=pw, browser_context=context)

    await state.close(close_browser_on_completion=False)

    pw.stop.assert_not_awaited()
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_release_driver_override_preserves_reuse() -> None:
    """An explicit release_driver=False wins over the creation-time marker."""
    pw = _pw_stub()
    state = RealBrowserState(pw=pw, browser_context=None, release_driver_on_close=True)

    await state.close(close_browser_on_completion=False, release_driver=False)

    pw.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_true_still_stops_driver_and_context() -> None:
    pw = _pw_stub()
    context = _context_stub()
    context.cookies = AsyncMock(return_value=[])
    state = RealBrowserState(pw=pw, browser_context=context, browser_artifacts=BrowserArtifacts())

    await state.close(close_browser_on_completion=True)

    pw.stop.assert_awaited_once()
    context.close.assert_awaited_once()


def _patch_create_browser_state(
    monkeypatch: pytest.MonkeyPatch,
    pw: MagicMock,
    *,
    context_result: tuple | None = None,
    raises: BaseException | None = None,
) -> AsyncMock:
    """Point ``_create_browser_state`` at a stub driver and a context factory that
    either returns a context tuple or raises (e.g. a connect_over_cdp failure)."""
    playwright_launcher = MagicMock()
    playwright_launcher.return_value.start = AsyncMock(return_value=pw)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.async_playwright", playwright_launcher)

    if raises is not None:
        create_browser_context = AsyncMock(side_effect=raises)
    else:
        create_browser_context = AsyncMock(return_value=context_result or (_context_stub(), BrowserArtifacts(), None))
    monkeypatch.setattr(
        "skyvern.webeye.real_browser_manager.BrowserContextFactory.create_browser_context",
        create_browser_context,
    )
    return create_browser_context


@pytest.mark.asyncio
async def test_create_browser_state_stops_driver_when_context_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A started local driver must be stopped if create_browser_context (e.g. a
    remote-CDP connect) fails, and the original failure must propagate."""
    pw = _pw_stub()
    _patch_create_browser_state(monkeypatch, pw, raises=RuntimeError("connect_over_cdp failed"))

    with pytest.raises(RuntimeError, match="connect_over_cdp failed"):
        await RealBrowserManager._create_browser_state(browser_address="http://192.0.2.10:9222")

    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_browser_state_cleanup_failure_does_not_mask_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If stopping the driver also fails, the original creation error still surfaces."""
    pw = _pw_stub()
    pw.stop = AsyncMock(side_effect=RuntimeError("driver stop boom"))
    _patch_create_browser_state(monkeypatch, pw, raises=ValueError("original create failure"))

    with pytest.raises(ValueError, match="original create failure"):
        await RealBrowserManager._create_browser_state()

    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_browser_state_success_retains_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path keeps the driver on the state and must not stop it early."""
    pw = _pw_stub()
    context = _context_stub()
    _patch_create_browser_state(monkeypatch, pw, context_result=(context, BrowserArtifacts(), None))

    created = await RealBrowserManager._create_browser_state()

    pw.stop.assert_not_awaited()
    assert created.pw is pw


@pytest.mark.asyncio
async def test_create_browser_state_stops_driver_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation during context creation must still release the started driver
    (CancelledError is BaseException, not caught by ``except Exception``)."""
    pw = _pw_stub()
    _patch_create_browser_state(monkeypatch, pw, raises=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await RealBrowserManager._create_browser_state()

    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_browser_state_hung_stop_does_not_block_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung driver stop() on the failure path must be time-bounded so it cannot
    stall the original creation error forever; the original error is re-raised."""
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.BROWSER_CLOSE_TIMEOUT", 0.05, raising=False)
    pw = _pw_stub()

    async def _hang(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(30)

    pw.stop = AsyncMock(side_effect=_hang)
    _patch_create_browser_state(monkeypatch, pw, raises=ValueError("original create failure"))

    with pytest.raises(ValueError, match="original create failure"):
        await asyncio.wait_for(RealBrowserManager._create_browser_state(), timeout=2)

    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_browser_state_marks_remote_cdp_states(monkeypatch: pytest.MonkeyPatch) -> None:
    pw = _pw_stub()
    playwright_launcher = MagicMock()
    playwright_launcher.return_value.start = AsyncMock(return_value=pw)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.async_playwright", playwright_launcher)

    create_browser_context = AsyncMock(return_value=(_context_stub(), BrowserArtifacts(), None))
    monkeypatch.setattr(
        "skyvern.webeye.real_browser_manager.BrowserContextFactory.create_browser_context",
        create_browser_context,
    )

    remote_state = await RealBrowserManager._create_browser_state(browser_address="http://192.0.2.10:9222")
    local_state = await RealBrowserManager._create_browser_state()

    assert isinstance(remote_state, RealBrowserState)
    assert isinstance(local_state, RealBrowserState)
    assert remote_state.release_driver_on_close is True
    assert local_state.release_driver_on_close is False


def _fake_browser_state() -> MagicMock:
    state = MagicMock()
    state.close = AsyncMock()
    state.browser_context = None
    state.browser_artifacts = BrowserArtifacts()
    return state


@pytest.mark.asyncio
async def test_cleanup_for_task_keeps_driver_for_persistent_session() -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["tsk_1"] = state

    await manager.cleanup_for_task(
        "tsk_1",
        close_browser_on_completion=False,
        browser_session_id="session_1",
        organization_id="org_1",
    )

    state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)


@pytest.mark.asyncio
async def test_cleanup_for_task_lets_state_decide_without_persistent_session() -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["tsk_1"] = state

    await manager.cleanup_for_task("tsk_1", close_browser_on_completion=False)

    state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=None)


@pytest.mark.asyncio
async def test_cleanup_for_workflow_run_keeps_driver_while_shared() -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["wr_child"] = state
    manager.pages["tsk_1"] = state
    manager.pages["wr_parent"] = state

    await manager.cleanup_for_workflow_run(
        "wr_child",
        ["tsk_1"],
        close_browser_on_completion=False,
    )

    # Both the workflow-run-level close and the task-level close observe the
    # parent's surviving reference and must not release the shared driver.
    for call in state.close.await_args_list:
        assert call.kwargs["release_driver"] is False
    assert "wr_parent" in manager.pages


@pytest.mark.asyncio
async def test_cleanup_for_workflow_run_final_close_lets_state_decide() -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["wr_1"] = state
    manager.pages["tsk_1"] = state

    await manager.cleanup_for_workflow_run(
        "wr_1",
        ["tsk_1"],
        close_browser_on_completion=False,
    )

    final_call = state.close.await_args_list[-1]
    assert final_call.kwargs["release_driver"] is None
    assert "wr_1" not in manager.pages
    assert "tsk_1" not in manager.pages


@pytest.mark.asyncio
async def test_cleanup_for_workflow_run_owned_vnc_session_has_one_teardown_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["wr_1"] = state
    manager.pages["tsk_1"] = state

    sessions_manager = MagicMock()
    sessions_manager.requires_local_vnc_display.return_value = True
    sessions_manager.close_session = AsyncMock()
    sessions_manager.release_browser_session = AsyncMock()
    monkeypatch.setattr(
        "skyvern.webeye.real_browser_manager.app.PERSISTENT_SESSIONS_MANAGER",
        sessions_manager,
    )

    await manager.cleanup_for_workflow_run(
        "wr_1",
        ["tsk_1"],
        close_browser_on_completion=True,
        browser_session_id="session_1",
        organization_id="org_1",
    )

    sessions_manager.close_session.assert_awaited_once_with("org_1", "session_1")
    sessions_manager.release_browser_session.assert_not_awaited()
    state.close.assert_not_awaited()
    assert "wr_1" not in manager.pages
    assert "tsk_1" not in manager.pages


@pytest.mark.asyncio
async def test_cleanup_for_workflow_run_caller_supplied_vnc_session_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["wr_1"] = state
    manager.pages["tsk_1"] = state

    sessions_manager = MagicMock()
    sessions_manager.requires_local_vnc_display.return_value = True
    sessions_manager.close_session = AsyncMock()
    sessions_manager.release_browser_session = AsyncMock()
    monkeypatch.setattr(
        "skyvern.webeye.real_browser_manager.app.PERSISTENT_SESSIONS_MANAGER",
        sessions_manager,
    )

    await manager.cleanup_for_workflow_run(
        "wr_1",
        ["tsk_1"],
        close_browser_on_completion=False,
        browser_session_id="session_1",
        organization_id="org_1",
    )

    sessions_manager.close_session.assert_not_awaited()
    sessions_manager.release_browser_session.assert_awaited_once_with("session_1", organization_id="org_1")
    assert state.close.await_args_list[0].kwargs == {
        "close_browser_on_completion": False,
        "release_driver": False,
    }
    assert state.close.await_args_list[-1].kwargs == {
        "close_browser_on_completion": False,
        "release_driver": False,
    }


@pytest.mark.asyncio
async def test_cleanup_for_workflow_run_cdp_persistent_session_call_shapes_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["wr_1"] = state
    manager.pages["tsk_1"] = state

    sessions_manager = MagicMock()
    sessions_manager.requires_local_vnc_display.return_value = False
    sessions_manager.close_session = AsyncMock()
    sessions_manager.release_browser_session = AsyncMock()
    monkeypatch.setattr(
        "skyvern.webeye.real_browser_manager.app.PERSISTENT_SESSIONS_MANAGER",
        sessions_manager,
    )

    await manager.cleanup_for_workflow_run(
        "wr_1",
        ["tsk_1"],
        close_browser_on_completion=True,
        browser_session_id="session_1",
        organization_id="org_1",
    )

    sessions_manager.close_session.assert_not_awaited()
    sessions_manager.release_browser_session.assert_awaited_once_with("session_1", organization_id="org_1")
    assert [call.kwargs for call in state.close.await_args_list] == [
        {"close_browser_on_completion": False, "release_driver": False},
        {"close_browser_on_completion": True, "release_driver": False},
    ]
