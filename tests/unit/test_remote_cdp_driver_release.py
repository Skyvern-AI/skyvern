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
import time
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from skyvern.exceptions import MissingBrowserStateForBrowserSession, MissingOrganizationForBrowserSession
from skyvern.webeye import browser_engine
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_engine import (
    BrowserEngineContext,
    BrowserEngineMetadata,
    BrowserEngineSelection,
    BrowserSourceNotSupportedByEngine,
)
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor
from skyvern.webeye.real_browser_manager import RealBrowserManager, _EngineSelectionOwner, canonical_run_key
from skyvern.webeye.real_browser_state import RealBrowserState


def _pw_stub() -> MagicMock:
    pw = MagicMock()
    pw.stop = AsyncMock()
    return pw


def _context_stub() -> MagicMock:
    context = MagicMock()
    context.close = AsyncMock()
    context._skyvern_cdp_download_interceptor = None
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
async def test_close_reused_context_preserves_download_interceptor() -> None:
    pw = _pw_stub()
    context = _context_stub()
    interceptor = MagicMock(disable=AsyncMock())
    context._skyvern_cdp_download_interceptor = interceptor
    state = RealBrowserState(pw=pw, browser_context=context)

    await state.close(close_browser_on_completion=False)

    interceptor.disable.assert_not_awaited()
    assert context._skyvern_cdp_download_interceptor is interceptor
    context.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_context_disables_download_interceptor_once() -> None:
    context = _context_stub()
    context.cookies = AsyncMock(return_value=[])
    interceptor = MagicMock(disable=AsyncMock())
    context._skyvern_cdp_download_interceptor = interceptor
    state = RealBrowserState(pw=_pw_stub(), browser_context=context)

    await state.close(close_browser_on_completion=True)
    await state.close(close_browser_on_completion=True)

    interceptor.disable.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_true_timeout_bounds_interceptor_drain_and_cleans_suspended_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pw = _pw_stub()
    context = _context_stub()
    interceptor = CDPDownloadInterceptor()
    interceptor._accepting_browser_downloads = True
    context._skyvern_cdp_download_interceptor = interceptor
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def suspended_handler(event: dict[str, object]) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finally:
            cleaned.set()

    monkeypatch.setattr("skyvern.webeye.real_browser_state.BROWSER_INTERCEPTOR_DISABLE_TIMEOUT", 0.05)
    monkeypatch.setattr("skyvern.webeye.real_browser_state.BROWSER_CLOSE_TIMEOUT", 0.05)
    monkeypatch.setattr(interceptor, "_handle_browser_download", suspended_handler)
    interceptor._schedule_browser_download_handler({"url": "https://example.test/download"})
    await started.wait()

    start = time.monotonic()
    state = RealBrowserState(pw=pw, browser_context=context)
    result = await asyncio.wait_for(state.close(True), timeout=0.5)
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 0.5
    await asyncio.wait_for(cancelled.wait(), timeout=0.5)
    assert not cleaned.is_set()
    assert len(interceptor._browser_download_tasks) == 1
    assert context._skyvern_cdp_download_interceptor is None
    # The bounded interceptor drain is detached independently, so later teardown steps
    # (context close, driver stop) still run instead of being starved by the stuck drain.
    context.close.assert_awaited_once()
    pw.stop.assert_awaited_once()

    release.set()
    await asyncio.wait_for(cleaned.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert not interceptor._browser_download_tasks


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


@pytest.mark.asyncio
async def test_close_true_cancellation_resistant_interceptor_disable_still_runs_provider_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The paid-provider cleanup must still be attempted exactly once even when disabling the
    # download interceptor is cancellation-resistant (ignores the cancel it receives when its
    # budget is exhausted). close() must return within an outer watchdog, and the stuck drain
    # must stay owned (its eventual exception retrieved) rather than orphaned.
    monkeypatch.setattr("skyvern.webeye.real_browser_state.BROWSER_INTERCEPTOR_DISABLE_TIMEOUT", 0.05)
    pw = _pw_stub()
    context = _context_stub()
    context.cookies = AsyncMock(return_value=[])
    cleanup = AsyncMock()

    started = asyncio.Event()
    entered_cancel = asyncio.Event()
    release = asyncio.Event()

    async def stuck_disable(_ctx: object) -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            entered_cancel.set()
            await release.wait()  # cancellation-resistant: ignore the cancel until released
            raise RuntimeError("drain boom after detach")

    monkeypatch.setattr("skyvern.webeye.real_browser_state.disable_download_interceptor_for_context", stuck_disable)

    unretrieved: list[dict] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context_: unretrieved.append(context_))
    try:
        state = RealBrowserState(pw=pw, browser_context=context, browser_cleanup=cleanup)
        start = time.monotonic()
        await asyncio.wait_for(state.close(True), timeout=1.0)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0
        cleanup.assert_awaited_once()  # provider cleanup ran despite the stuck drain
        context.close.assert_awaited_once()
        pw.stop.assert_awaited_once()

        await asyncio.wait_for(entered_cancel.wait(), timeout=1.0)  # the drain was cancelled (best-effort reclaim)
        release.set()
        await asyncio.sleep(0)  # let the detached drain finish and its done-callback retrieve the exception
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert not any("never retrieved" in str(c.get("message", "")) for c in unretrieved)


@pytest.mark.asyncio
async def test_close_true_detached_teardown_task_is_strongly_held_until_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A detached, cancellation-resistant teardown phase must be strongly referenced for its whole
    # remaining life: asyncio only holds tasks weakly, so without an owner set a still-pending drain
    # could be GC'd ("Task was destroyed but it is pending!"). It must be in the owner set right after
    # detach and gone only once it actually completes — leaking neither a task nor an exception.
    monkeypatch.setattr("skyvern.webeye.real_browser_state.BROWSER_INTERCEPTOR_DISABLE_TIMEOUT", 0.05)
    pw = _pw_stub()
    context = _context_stub()
    context.cookies = AsyncMock(return_value=[])
    cleanup = AsyncMock()

    entered_cancel = asyncio.Event()
    release = asyncio.Event()

    async def stuck_disable(_ctx: object) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            entered_cancel.set()
            await release.wait()  # cancellation-resistant: outlive close()
            raise RuntimeError("drain boom after detach")

    monkeypatch.setattr("skyvern.webeye.real_browser_state.disable_download_interceptor_for_context", stuck_disable)

    unretrieved: list[dict] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context_: unretrieved.append(context_))
    try:
        state = RealBrowserState(pw=pw, browser_context=context, browser_cleanup=cleanup)
        await asyncio.wait_for(state.close(True), timeout=1.0)

        await asyncio.wait_for(entered_cancel.wait(), timeout=1.0)
        assert len(state._detached_teardown_tasks) == 1  # detached drain is strongly held while pending

        release.set()
        detached = next(iter(state._detached_teardown_tasks))
        await asyncio.wait_for(asyncio.shield(asyncio.gather(detached, return_exceptions=True)), timeout=1.0)
        await asyncio.sleep(0)  # let the done-callback discard the finished task

        assert state._detached_teardown_tasks == set()  # discarded only after it actually completed
    finally:
        loop.set_exception_handler(previous_handler)

    assert not any("never retrieved" in str(c.get("message", "")) for c in unretrieved)


@pytest.mark.asyncio
async def test_close_true_context_close_hang_does_not_suppress_provider_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hung context close must not starve the paid-provider cleanup: the context-teardown phase is
    # bounded independently, so cleanup still runs exactly once after the teardown budget elapses.
    monkeypatch.setattr("skyvern.webeye.real_browser_state.BROWSER_CLOSE_TIMEOUT", 0.05)
    pw = _pw_stub()
    context = _context_stub()
    context.cookies = AsyncMock(return_value=[])

    async def hang(*_a: object, **_k: object) -> None:
        await asyncio.sleep(3600)

    context.close = AsyncMock(side_effect=hang)
    cleanup = AsyncMock()
    state = RealBrowserState(pw=pw, browser_context=context, browser_cleanup=cleanup)

    await asyncio.wait_for(state.close(True), timeout=1.0)

    cleanup.assert_awaited_once()
    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_true_persist_cookies_failure_does_not_suppress_provider_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A raising cookie-persistence step (an "earlier teardown callback raises") must not suppress cleanup.
    pw = _pw_stub()
    context = _context_stub()
    monkeypatch.setattr(
        "skyvern.webeye.real_browser_state.persist_session_cookies",
        AsyncMock(side_effect=RuntimeError("persist boom")),
    )
    cleanup = AsyncMock()
    state = RealBrowserState(
        pw=pw,
        browser_context=context,
        browser_artifacts=BrowserArtifacts(browser_session_dir="/tmp/does-not-need-to-exist"),
        browser_cleanup=cleanup,
    )

    await state.close(close_browser_on_completion=True)

    cleanup.assert_awaited_once()
    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_true_browser_cleanup_exception_is_handled_not_leaked() -> None:
    # A raising provider cleanup must be caught (best-effort teardown) and must not leak or block driver stop.
    pw = _pw_stub()
    context = _context_stub()
    context.cookies = AsyncMock(return_value=[])
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup boom"))
    state = RealBrowserState(pw=pw, browser_context=context, browser_cleanup=cleanup)

    await state.close(close_browser_on_completion=True)  # must not raise

    cleanup.assert_awaited_once()
    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_true_runs_teardown_phases_in_order() -> None:
    pw = _pw_stub()
    context = _context_stub()
    context.cookies = AsyncMock(return_value=[])
    cleanup = AsyncMock()
    interceptor = MagicMock(disable=AsyncMock())
    context._skyvern_cdp_download_interceptor = interceptor
    state = RealBrowserState(pw=pw, browser_context=context, browser_cleanup=cleanup)

    order: list[str] = []
    callback = AsyncMock(side_effect=lambda: order.append("callback"))
    state.add_on_close(callback)
    interceptor.disable.side_effect = lambda: order.append("disable_interceptor")
    context.close.side_effect = lambda: order.append("context_close")
    cleanup.side_effect = lambda: order.append("browser_cleanup")
    pw.stop.side_effect = lambda: order.append("stop_driver")

    await state.close(close_browser_on_completion=True)

    assert order == ["disable_interceptor", "callback", "context_close", "browser_cleanup", "stop_driver"]


@pytest.mark.asyncio
async def test_close_false_preserves_reuse_and_does_not_invoke_cleanup() -> None:
    pw = _pw_stub()
    context = _context_stub()
    interceptor = MagicMock(disable=AsyncMock())
    context._skyvern_cdp_download_interceptor = interceptor
    cleanup = AsyncMock()
    state = RealBrowserState(pw=pw, browser_context=context, browser_cleanup=cleanup)

    await state.close(close_browser_on_completion=False)

    cleanup.assert_not_awaited()
    interceptor.disable.assert_not_awaited()
    context.close.assert_not_awaited()
    pw.stop.assert_not_awaited()
    assert context._skyvern_cdp_download_interceptor is interceptor


@pytest.mark.asyncio
async def test_close_true_runs_provider_cleanup_exactly_once_across_reentry() -> None:
    # Re-entrant close() must not stop/delete the paid provider a second time (avoid double-cleanup).
    pw = _pw_stub()
    context = _context_stub()
    context.cookies = AsyncMock(return_value=[])
    cleanup = AsyncMock()
    state = RealBrowserState(pw=pw, browser_context=context, browser_cleanup=cleanup)

    await state.close(close_browser_on_completion=True)
    await state.close(close_browser_on_completion=True)

    cleanup.assert_awaited_once()


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
    monkeypatch.setattr("skyvern.webeye.browser_engine.async_playwright", playwright_launcher)

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
        await RealBrowserManager()._create_browser_state(browser_address="http://192.0.2.10:9222")

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
        await RealBrowserManager()._create_browser_state()

    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_browser_state_success_retains_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path keeps the driver on the state and must not stop it early."""
    pw = _pw_stub()
    context = _context_stub()
    _patch_create_browser_state(monkeypatch, pw, context_result=(context, BrowserArtifacts(), None))

    created = await RealBrowserManager()._create_browser_state()

    pw.stop.assert_not_awaited()
    assert created.pw is pw


@pytest.mark.asyncio
async def test_create_browser_state_stops_driver_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation during context creation must still release the started driver
    (CancelledError is BaseException, not caught by ``except Exception``)."""
    pw = _pw_stub()
    _patch_create_browser_state(monkeypatch, pw, raises=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await RealBrowserManager()._create_browser_state()

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
        await asyncio.wait_for(RealBrowserManager()._create_browser_state(), timeout=2)

    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_browser_state_marks_remote_cdp_states(monkeypatch: pytest.MonkeyPatch) -> None:
    pw = _pw_stub()
    playwright_launcher = MagicMock()
    playwright_launcher.return_value.start = AsyncMock(return_value=pw)
    monkeypatch.setattr("skyvern.webeye.browser_engine.async_playwright", playwright_launcher)

    create_browser_context = AsyncMock(return_value=(_context_stub(), BrowserArtifacts(), None))
    monkeypatch.setattr(
        "skyvern.webeye.real_browser_manager.BrowserContextFactory.create_browser_context",
        create_browser_context,
    )

    remote_state = await RealBrowserManager()._create_browser_state(browser_address="http://192.0.2.10:9222")
    local_state = await RealBrowserManager()._create_browser_state()

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
    for close_call in state.close.await_args_list:
        assert close_call.kwargs["release_driver"] is False
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


class _FakeEngineError(Exception):
    pass


def _fake_selection(name: str, *, allowed_sources: frozenset[str] | None = None) -> BrowserEngineSelection:
    async def _ok_start() -> object:
        return object()

    return BrowserEngineSelection(
        name=name,
        start_driver=_ok_start,
        error_type=_FakeEngineError,
        timeout_error_type=_FakeEngineError,
        metadata=BrowserEngineMetadata(name=name, version="0.0.0", allowed_browser_sources=allowed_sources),
        selection_reason="test",
    )


def _ctx(run_id: str | None, source: str | None = "local-browser") -> BrowserEngineContext:
    return BrowserEngineContext(workflow_run_id=run_id, browser_source=source)


async def _seed_engine_owner(manager: RealBrowserManager, run_key: str, name: str) -> None:
    """Install a completed engine owner for ``run_key`` (mirrors a resolved run)."""
    selection = _fake_selection(name)
    browser_engine.set_browser_engine_resolver(lambda ctx: _coro(selection))
    await manager.get_or_resolve_engine_selection(run_key=run_key, context=_ctx(run_key, None))


@pytest.fixture()
def _restore_resolver():
    yield
    browser_engine.reset_browser_engine_resolver()


@pytest.mark.asyncio
async def test_manager_pins_one_engine_per_run_and_reuses_on_recreation(_restore_resolver: None) -> None:
    calls = {"n": 0}

    async def counting_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        calls["n"] += 1
        return _fake_selection(f"engine-{calls['n']}")

    browser_engine.set_browser_engine_resolver(counting_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source=None)  # unrestricted engine allows None

    first = await manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx)
    second = await manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx)
    assert first is second
    assert calls["n"] == 1

    other = await manager.get_or_resolve_engine_selection(run_key="wr_2", context=_ctx("wr_2"))
    assert other is not first
    assert calls["n"] == 2

    await manager._drop_engine_owner("wr_1")
    third = await manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx)
    assert third is not first
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_concurrent_first_acquisitions_resolve_once_and_share_selection(_restore_resolver: None) -> None:
    # check -> await resolver -> store must be single-flighted per run key.
    calls = {"n": 0}

    async def slow_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        calls["n"] += 1
        await asyncio.sleep(0)  # force the two coroutines to interleave inside the resolver window
        return _fake_selection(f"engine-{calls['n']}")

    browser_engine.set_browser_engine_resolver(slow_resolver)
    manager = RealBrowserManager()
    same = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    a, b = await asyncio.gather(
        manager.get_or_resolve_engine_selection(run_key="wr_1", context=same),
        manager.get_or_resolve_engine_selection(run_key="wr_1", context=same),
    )
    assert a is b
    assert calls["n"] == 1  # resolver invoked exactly once for the shared key

    # A different key resolves independently, concurrently, without blocking on the first key.
    other = await manager.get_or_resolve_engine_selection(run_key="wr_2", context=_ctx("wr_2"))
    assert other is not a
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_failed_first_resolution_leaves_no_orphan_owner(_restore_resolver: None) -> None:
    # A resolver failure must leave no owner (not even an unlocked entry), so retries resolve and failed keys can't pile up.
    attempts = {"n": 0}

    async def flaky_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("resolver boom")
        return _fake_selection("engine-ok")

    browser_engine.set_browser_engine_resolver(flaky_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    with pytest.raises(RuntimeError, match="resolver boom"):
        await manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx)
    assert "wr_1" not in manager._engine_owners

    recovered = await manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx)
    assert recovered.name == "engine-ok"


@pytest.mark.asyncio
async def test_concurrent_failed_first_resolution_leaves_no_orphan_owner(_restore_resolver: None) -> None:
    # Two waiters on the same key whose shared resolution fails: both observe the failure and neither orphans an owner.
    async def boom_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        await asyncio.sleep(0)
        raise RuntimeError("resolver boom")

    browser_engine.set_browser_engine_resolver(boom_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    results = await asyncio.gather(
        manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx),
        manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx),
        return_exceptions=True,
    )
    assert all(isinstance(r, RuntimeError) for r in results)
    assert manager._engine_owners == {}


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_abort_shared_resolution(_restore_resolver: None) -> None:
    # Cancelling one waiter must not abort the shared resolution nor drop the owner the survivor still needs.
    calls = {"n": 0}
    gate = asyncio.Event()

    async def gated_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        calls["n"] += 1
        await gate.wait()
        return _fake_selection(f"engine-{calls['n']}")

    browser_engine.set_browser_engine_resolver(gated_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    survivor = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
    victim = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
    await asyncio.sleep(0)  # let both attach to the shared owner task
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim
    gate.set()
    selection = await survivor
    assert selection.name == "engine-1"
    assert calls["n"] == 1  # the shared resolution ran exactly once despite the cancelled waiter

    # A waiter cancelled AFTER the resolution succeeds must not evict the healthy owner; that is the done-callback's job.
    gate.clear()
    ctx2 = BrowserEngineContext(workflow_run_id="wr_2", browser_source="local-browser")
    keeper = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_2", context=ctx2))
    late = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_2", context=ctx2))
    await asyncio.sleep(0)  # both attach to the shared owner task
    manager._engine_owners["wr_2"].task.add_done_callback(lambda _t: late.cancel())  # cancel in success tick
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await late
    assert (await keeper).name == "engine-2"
    assert "wr_2" in manager._engine_owners  # successful owner survived the late waiter cancel
    assert (await manager.get_or_resolve_engine_selection(run_key="wr_2", context=ctx2)) is keeper.result()
    assert calls["n"] == 2  # wr_1 once, wr_2 once; the late cancel forced no re-resolution


@pytest.mark.asyncio
async def test_terminal_cleanup_racing_resolver_prevents_resurrection(_restore_resolver: None) -> None:
    # Cleanup while a resolver is in flight must cancel it, leave no owner, not resurrect state; a later acquire resolves fresh.
    calls = {"n": 0}
    gate = asyncio.Event()

    async def gated_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        calls["n"] += 1
        await gate.wait()
        return _fake_selection(f"engine-{calls['n']}")

    browser_engine.set_browser_engine_resolver(gated_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    inflight = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
    await asyncio.sleep(0)  # resolver is now attached and blocked on the gate
    await manager._drop_engine_owner("wr_1")  # terminal cleanup awaits the racing resolver to termination
    with pytest.raises(asyncio.CancelledError):
        await inflight
    # The cancelled resolver stored nothing and did not reinstall an owner: no resurrection.
    assert "wr_1" not in manager._engine_owners

    gate.set()  # a fresh resolution for the reused key resolves cleanly under a brand-new owner
    fresh = await manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx)
    assert manager._engine_owners["wr_1"].task.result() is fresh


@pytest.mark.asyncio
async def test_null_run_key_is_ephemeral_and_not_pinned(_restore_resolver: None) -> None:
    calls = {"n": 0}

    async def counting_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        calls["n"] += 1
        return _fake_selection(f"engine-{calls['n']}")

    browser_engine.set_browser_engine_resolver(counting_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(browser_source="local-browser")

    first = await manager.get_or_resolve_engine_selection(run_key=None, context=ctx)
    second = await manager.get_or_resolve_engine_selection(run_key=None, context=ctx)
    assert first is not second  # no durable identity => resolved each time, never cached
    assert manager._engine_owners == {}


@pytest.mark.asyncio
async def test_workflow_owned_task_pins_engine_under_workflow_run_id(
    monkeypatch: pytest.MonkeyPatch, _restore_resolver: None
) -> None:
    # The task path passes engine_run_key=canonical_run_key(...); a workflow-owned task pins under workflow_run_id only.
    assert canonical_run_key(workflow_run_id="wr_1", task_id="tsk_1") == "wr_1"
    assert canonical_run_key(task_id="tsk_1") == "tsk_1"

    _patch_create_browser_state(monkeypatch, _pw_stub(), context_result=(_context_stub(), BrowserArtifacts(), None))
    manager = RealBrowserManager()
    await manager._create_browser_state(
        task_id="tsk_1", engine_run_key=canonical_run_key(workflow_run_id="wr_1", task_id="tsk_1")
    )
    assert "wr_1" in manager._engine_owners
    assert "tsk_1" not in manager._engine_owners


@pytest.mark.parametrize("unsupported_source", ["local-browser", None])
@pytest.mark.asyncio
async def test_manager_capability_gate_applies_even_on_cache_hit(
    _restore_resolver: None, unsupported_source: str | None
) -> None:
    # A restricted engine rejects an unsupported cached source (incl. None, failed closed); the pinned owner survives.
    restricted = _fake_selection("restricted-engine", allowed_sources=frozenset({"cdp-connection-browser"}))
    browser_engine.set_browser_engine_resolver(lambda ctx: _coro(restricted))
    manager = RealBrowserManager()

    ok = await manager.get_or_resolve_engine_selection(
        run_key="wr_x",
        context=BrowserEngineContext(workflow_run_id="wr_x", browser_source="cdp-connection-browser"),
    )
    assert ok is restricted
    owner = manager._engine_owners["wr_x"]

    with pytest.raises(BrowserSourceNotSupportedByEngine):
        await manager.get_or_resolve_engine_selection(
            run_key="wr_x",
            context=BrowserEngineContext(workflow_run_id="wr_x", browser_source=unsupported_source),
        )
    assert manager._engine_owners.get("wr_x") is owner


@pytest.mark.asyncio
async def test_cleanup_drops_pinned_engine_owner_for_run(_restore_resolver: None) -> None:
    manager = RealBrowserManager()
    await _seed_engine_owner(manager, "wr_1", "engine-1")
    await _seed_engine_owner(manager, "tsk_1", "engine-1")
    manager.pages["wr_1"] = _fake_browser_state()

    await manager.cleanup_for_workflow_run("wr_1", ["tsk_1"], close_browser_on_completion=False)

    assert "wr_1" not in manager._engine_owners
    assert "tsk_1" not in manager._engine_owners


@pytest.mark.asyncio
async def test_sole_waiter_cancelled_then_resolver_fails_reaps_owner(_restore_resolver: None) -> None:
    # MF1: the sole waiter is cancelled while the shared resolution is still pending; when the resolver
    # LATER fails with no live waiter, the owner-managed done-callback must reap the failed owner and
    # consume its exception — no lingering owner, no "task exception was never retrieved" warning.
    gate = asyncio.Event()

    async def late_failing_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        await gate.wait()
        raise RuntimeError("late boom")

    browser_engine.set_browser_engine_resolver(late_failing_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    waiter: asyncio.Future | None = None
    owner_task: asyncio.Task | None = None
    reaper_consumed_exception = False
    try:
        waiter = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
        # A single semantic yield lets the just-scheduled waiter run to its first suspension: the manager
        # installs the owner and the waiter parks on its shared task (both happen before this yield returns).
        await asyncio.sleep(0)
        # Observe the reaping directly instead of guessing scheduler turns: our done-callback is registered
        # after the manager's own _reap_failed_owner, so it fires strictly after the reap has already run.
        owner_task = manager._engine_owners["wr_1"].task
        reaped = asyncio.Event()
        owner_task.add_done_callback(lambda _t: reaped.set())

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        # The waiter's cancellation must NOT remove the owner while the shared task is still pending.
        assert "wr_1" in manager._engine_owners

        gate.set()  # the resolver now fails with no live waiter to observe it
        await reaped.wait()  # the shared task finished and its reaping done-callback has run
        # asyncio clears a task's private _log_traceback flag exactly when its exception is retrieved, and
        # the reaping callback is the only retriever at this point. Capture it BEFORE the teardown drains
        # owner_task: a gather(return_exceptions=True) would itself retrieve the exception and clear the
        # flag, masking a _reap_failed_owner that evicted the owner WITHOUT calling task.exception(). This
        # is deterministic — unlike observing a "Task exception was never retrieved" warning, which only
        # fires at non-deterministic GC time.
        reaper_consumed_exception = owner_task._log_traceback is False
    finally:
        for task in (waiter, owner_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in (waiter, owner_task) if t is not None], return_exceptions=True)

    # The done-callback reaped the failed owner AND consumed its exception (both are load-bearing).
    assert "wr_1" not in manager._engine_owners
    assert reaper_consumed_exception


@pytest.mark.asyncio
async def test_stale_owner_completion_does_not_evict_newer_owner(_restore_resolver: None) -> None:
    # MF1 identity guard: a stale owner whose resolution finished exceptionally AFTER it was already
    # replaced (terminal drop + reacquire under the reused key) must not evict the NEWER owner when its
    # late done-callback finally fires.
    manager = RealBrowserManager()

    async def _boom() -> BrowserEngineSelection:
        raise RuntimeError("stale")

    stale_task = asyncio.ensure_future(_boom())
    await asyncio.gather(stale_task, return_exceptions=True)  # drive the stale resolution to failure
    stale_owner = _EngineSelectionOwner(stale_task)

    await _seed_engine_owner(manager, "wr_1", "engine-new")  # the newer, current owner for the reused key
    newer_owner = manager._engine_owners["wr_1"]

    manager._reap_failed_owner("wr_1", stale_owner, stale_task)  # the stale completion fires late

    assert manager._engine_owners["wr_1"] is newer_owner  # the newer owner survived the stale callback


@pytest.mark.asyncio
async def test_terminal_drop_awaits_suppressing_resolver_before_second_resolver_starts(
    _restore_resolver: None,
) -> None:
    # MF2: terminal cleanup must await the in-flight resolver to definitive termination — even one that
    # SUPPRESSES cancellation — before returning, so a second same-key resolver never runs concurrently
    # with the first. The concurrent first waiter coalesces onto the single resolution.
    live = {"now": 0, "max": 0, "starts": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def suppressing_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        live["starts"] += 1
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # suppress the cancellation and keep unwinding cooperatively
        finally:
            live["now"] -= 1
        return _fake_selection("engine-1")

    browser_engine.set_browser_engine_resolver(suppressing_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    first: asyncio.Future | None = None
    owner_task: asyncio.Task | None = None
    drop_task: asyncio.Future | None = None
    try:
        first = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
        await started.wait()  # resolver #1 is running
        owner_task = manager._engine_owners["wr_1"].task  # the single in-flight resolution
        assert live["now"] == 1

        # The drop must await resolver #1 to definitive termination (suppression and all). If it regressed to
        # await the resolver WITHOUT cancelling it, resolver #1 would stay gated on `release` — set only in
        # the finally, unreachable from here — and this await would hang the shard (no repo-wide pytest
        # timeout). Race the drop against a non-cancelling timer so the regression fails cleanly. A watchdog
        # rather than asyncio.wait_for: the resolver suppresses cancellation, which defeats wait_for's own
        # timeout-cancel.
        drop_task = asyncio.ensure_future(manager._drop_engine_owner("wr_1"))
        timer = asyncio.ensure_future(asyncio.sleep(5.0))
        try:
            done, _ = await asyncio.wait({drop_task, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        assert drop_task in done  # the drop returned; a non-cancelling regression would hang here instead
        await drop_task  # re-raise any exception / retrieve the result
        assert live["now"] == 0  # resolver #1 has definitively terminated before the drop returned
        assert "wr_1" not in manager._engine_owners
        assert (await first).name == "engine-1"  # the coalesced first waiter got the single resolution's value

        release.set()
        second = await manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx)
        assert second.name == "engine-1"
        assert live["starts"] == 2  # a brand-new resolver ran for the reused key...
        assert live["max"] == 1  # ...but never concurrently with the first
    finally:
        # Failure-safe teardown: if any assertion above trips while resolver #1 is still gated on
        # release, ungate it and reap both the coalesced waiter and the owner task so neither survives
        # to contaminate a later test.
        release.set()
        for task in (first, owner_task, drop_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in (first, owner_task, drop_task) if t is not None], return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_cancelled_mid_drop_keeps_owner_and_blocks_second_resolver(
    _restore_resolver: None,
) -> None:
    # MF2 (cancellation variant): if terminal cleanup is ITSELF cancelled while a resolver suppresses its
    # initial cancellation and stays gated, the cancellation must propagate to the caller, the terminal
    # owner must stay registered (no resolver #2 starts alongside #1), and only once resolver #1 finally
    # completes may a fresh owner/resolver install — at most one live resolver per key throughout.
    live = {"now": 0, "max": 0, "starts": 0}
    started = asyncio.Event()
    release = asyncio.Event()
    suppressed = asyncio.Event()

    async def gated_suppressing_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        live["starts"] += 1
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            suppressed.set()  # signal we absorbed the drop's cancel before re-gating (deterministic readiness)
            await release.wait()  # suppress the initial cancellation but stay gated until released
        finally:
            live["now"] -= 1
        return _fake_selection("engine-1")

    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")
    await _seed_engine_owner(manager, "wr_other", "engine-other")  # unrelated key must stay independent
    browser_engine.set_browser_engine_resolver(gated_suppressing_resolver)

    unretrieved: list[dict] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    first: asyncio.Future | None = None
    drop: asyncio.Future | None = None
    second: asyncio.Future | None = None
    owner1_task: asyncio.Task | None = None
    try:
        first = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
        await started.wait()  # resolver #1 is running
        owner1 = manager._engine_owners["wr_1"]
        owner1_task = owner1.task

        drop = asyncio.ensure_future(manager._drop_engine_owner("wr_1"))
        # Bounded readiness wait: if _drop_engine_owner regressed to mark the owner terminal without
        # cancelling resolver #1, the resolver would never enter its CancelledError handler and never set
        # `suppressed`, hanging this wait forever (there is no repo-wide pytest timeout). wait_for bounds it
        # reliably here because `suppressed.wait()` is a plain cancellable Event wait — not a cancellation-
        # swallowing task — so on timeout it fails the test cleanly instead of hanging the shard.
        await asyncio.wait_for(suppressed.wait(), timeout=5.0)
        assert owner1.terminal is True
        assert live["now"] == 1  # resolver #1 suppressed the cancel and is still gated

        drop.cancel()  # externally cancel the cleanup task itself
        with pytest.raises(asyncio.CancelledError):
            await drop  # cancellation propagates to the caller, never swallowed into a successful drop

        # The terminal owner stays registered and resolver #1 is still alive.
        assert manager._engine_owners.get("wr_1") is owner1
        assert live["now"] == 1

        # A same-key acquisition must NOT start resolver #2 while resolver #1 lives. A single semantic
        # yield lets the just-scheduled acquirer run to its first suspension (parking on the terminal
        # owner); if it wrongly started resolver #2 that would already show up in live["starts"] below.
        second = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
        await asyncio.sleep(0)
        assert live["starts"] == 1  # no resolver #2 began
        assert not second.done()
        assert manager._engine_owners.get("wr_1") is owner1  # still the terminal owner, not superseded

        release.set()  # resolver #1 finally completes
        result = await second  # the acquisition now installs and awaits a fresh resolver #2
        # Awaiting the acquirer already drove resolver #1 to completion and resolver #2 to its result;
        # draining both the coalesced waiter and resolver #1's task retrieves their outcomes explicitly.
        await asyncio.gather(first, owner1_task, return_exceptions=True)
    finally:
        # Failure-safe teardown: resolver #1 suppresses its initial cancel and re-gates on `release`,
        # so cancelling/draining alone would hang forever. Ungate it first, then reap first/drop/second
        # and the owner task so none survives to contaminate a later test.
        release.set()
        for task in (first, drop, second, owner1_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in (first, drop, second, owner1_task) if t is not None], return_exceptions=True)
        loop.set_exception_handler(previous_handler)

    assert result.name == "engine-1"
    assert live["starts"] == 2  # a brand-new resolver ran for the reused key...
    assert live["max"] == 1  # ...never concurrently with the first (max one live resolver per key)
    fresh = manager._engine_owners["wr_1"]
    assert fresh is not owner1 and fresh.task.result() is result  # fresh, non-terminal owner installed
    assert manager._engine_owners["wr_other"].task.result().name == "engine-other"  # unrelated key intact
    assert not any("never retrieved" in str(c.get("message", "")) for c in unretrieved)


@pytest.mark.asyncio
async def test_acquirer_cancelled_while_waiting_out_terminal_owner(_restore_resolver: None) -> None:
    # An acquirer that arrives during terminal teardown waits the dying owner out. If THAT acquirer is
    # itself cancelled, it must propagate its own CancelledError — not swallow it, delete the still-
    # running terminal owner, and install resolver #2 in its place (which would hang the acquirer and
    # break single-flight). The terminal owner and its lone live resolver must survive untouched.
    live = {"now": 0, "starts": 0}
    started = asyncio.Event()
    release = asyncio.Event()
    suppressed = asyncio.Event()

    async def gated_suppressing_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        live["starts"] += 1
        live["now"] += 1
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            suppressed.set()  # signal we absorbed the drop's cancel before re-gating (deterministic readiness)
            await release.wait()  # suppress the initial cancel and stay gated
        finally:
            live["now"] -= 1
        return _fake_selection("engine-1")

    browser_engine.set_browser_engine_resolver(gated_suppressing_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    first: asyncio.Future | None = None
    drop: asyncio.Future | None = None
    acquirer: asyncio.Future | None = None
    owner1_task: asyncio.Task | None = None
    try:
        first = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
        await started.wait()
        owner1 = manager._engine_owners["wr_1"]
        owner1_task = owner1.task
        reaped = asyncio.Event()
        # Registered after the manager's own _reap_failed_owner, so it fires strictly after the reap ran.
        owner1_task.add_done_callback(lambda _t: reaped.set())

        drop = asyncio.ensure_future(manager._drop_engine_owner("wr_1"))
        # Bounded readiness wait (see the mid-drop test): a drop that marks terminal without cancelling
        # resolver #1 would never set `suppressed`; wait_for bounds this plain Event wait so the regression
        # fails cleanly instead of hanging the shard.
        await asyncio.wait_for(suppressed.wait(), timeout=5.0)
        drop.cancel()  # leave owner1 terminal + resolver #1 still gated
        with pytest.raises(asyncio.CancelledError):
            await drop

        acquirer = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx))
        # A single semantic yield lets the just-scheduled acquirer run to its first suspension: it parks
        # waiting the terminal owner out. Any wrongful resolver #2 would already show in live["starts"].
        await asyncio.sleep(0)
        acquirer.cancel()
        # Non-cancelling watchdog: under the guarded regression the acquirer swallows its own cancel and
        # loops back to re-park on the still-gated terminal owner, so it never settles. Awaiting it to
        # completion — even via asyncio.wait_for, which must itself await the swallowed cancellation to
        # land — would hang the shard (there is no repo-wide pytest timeout). Race it against a timer we
        # never cancel for its timing, so a non-settling acquirer trips a clean assertion instead of hanging.
        timer = asyncio.ensure_future(asyncio.sleep(5.0))
        try:
            done, _ = await asyncio.wait({acquirer, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        assert acquirer in done and acquirer.cancelled()  # its OWN cancellation propagated, not swallowed

        assert manager._engine_owners.get("wr_1") is owner1  # still-running terminal owner untouched
        assert live["now"] == 1
        assert live["starts"] == 1  # no resolver #2 was installed in the terminal owner's place

        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await reaped.wait()  # resolver #1 finished and owner1's reaping done-callback has run
    finally:
        # Failure-safe teardown: resolver #1 suppresses its initial cancel and re-gates on `release`,
        # so cancelling/draining alone would hang forever. Ungate it first, then reap first/drop/acquirer
        # and the owner task so none survives to contaminate a later test.
        release.set()
        for task in (first, drop, acquirer, owner1_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *[t for t in (first, drop, acquirer, owner1_task) if t is not None], return_exceptions=True
        )
    assert "wr_1" not in manager._engine_owners  # owner1 reaped once its resolver finished


@pytest.mark.asyncio
async def test_cleanup_for_script_reclaims_page_and_owner_to_baseline(_restore_resolver: None) -> None:
    # MUST_FIX 2: many distinct script ids populate pages + engine owners; terminal cleanup of each
    # must return both maps to baseline. Cleanup is idempotent (a second call is a harmless no-op).
    manager = RealBrowserManager()
    script_ids = [f"scr_{i}" for i in range(25)]
    for sid in script_ids:
        await _seed_engine_owner(manager, sid, "engine-1")
        manager.pages[sid] = _fake_browser_state()
    assert len(manager._engine_owners) == len(script_ids)
    assert len(manager.pages) == len(script_ids)

    for sid in script_ids:
        await manager.cleanup_for_script(sid, close_browser_on_completion=False)
    assert await manager.cleanup_for_script(script_ids[0], close_browser_on_completion=False) is None

    assert manager._engine_owners == {}
    assert manager.pages == {}


@pytest.mark.asyncio
async def test_cleanup_for_script_releases_persistent_session_without_closing_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["scr_1"] = state
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock()
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)

    # Production default close_browser_on_completion=True: a persistent session is still only released.
    await manager.cleanup_for_script(
        "scr_1", close_browser_on_completion=True, browser_session_id="session_1", organization_id="org_1"
    )

    state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    sessions.release_browser_session.assert_awaited_once_with("session_1", organization_id="org_1")
    assert "scr_1" not in manager.pages


@pytest.mark.asyncio
async def test_cleanup_for_script_stops_tracing_before_browser_close(tmp_path) -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    state.browser_context = MagicMock()
    state.browser_context.tracing.stop = AsyncMock()
    state.browser_artifacts = BrowserArtifacts(traces_dir=str(tmp_path))
    manager.pages["scr_1"] = state
    calls = MagicMock()
    calls.attach_mock(state.browser_context.tracing.stop, "stop_tracing")
    calls.attach_mock(state.close, "close")

    await manager.cleanup_for_script("scr_1")

    state.browser_context.tracing.stop.assert_awaited_once_with(path=f"{tmp_path}/scr_1.zip")
    state.close.assert_awaited_once()
    assert calls.method_calls == [
        call.stop_tracing(path=f"{tmp_path}/scr_1.zip"),
        call.close(close_browser_on_completion=True, release_driver=None),
    ]


@pytest.mark.asyncio
async def test_cleanup_for_script_tracing_failure_does_not_skip_close_or_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    manager = RealBrowserManager()
    state = _fake_browser_state()
    state.browser_context = MagicMock()
    state.browser_context.tracing.stop = AsyncMock(side_effect=RuntimeError("trace boom"))
    state.browser_artifacts = BrowserArtifacts(traces_dir=str(tmp_path))
    manager.pages["scr_1"] = state
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock()
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)

    await manager.cleanup_for_script("scr_1", browser_session_id="session_1", organization_id="org_1")

    # Persistent session: close is forced reusable (False) even though the default is True.
    state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    sessions.release_browser_session.assert_awaited_once_with("session_1", organization_id="org_1")


@pytest.mark.asyncio
async def test_cleanup_for_script_warns_when_session_has_no_organization(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    log = MagicMock()
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.LOG", log)
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)

    await manager.cleanup_for_script("scr_1", browser_session_id="session_1")

    log.warning.assert_called_once_with(
        "Organization ID not specified, cannot release browser session", script_id="scr_1"
    )
    sessions.release_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_for_script_swallows_persistent_session_release_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MF3: cleanup is best-effort ("errors are logged, not raised"). A persistent-session release
    # failure must be caught and logged so it cannot escape run_script's finally and mask the script's
    # own exception. Cleanup still reclaims the page.
    manager = RealBrowserManager()
    manager.pages["scr_1"] = _fake_browser_state()
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(side_effect=RuntimeError("release boom"))
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)

    result = await manager.cleanup_for_script(
        "scr_1", close_browser_on_completion=False, browser_session_id="session_1", organization_id="org_1"
    )

    sessions.release_browser_session.assert_awaited_once()
    assert "scr_1" not in manager.pages
    assert result is not None


@pytest.mark.asyncio
async def test_get_or_create_for_script_uses_real_org_for_persistent_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    # MF2: acquisition looks the session up under the REAL organization_id (the symmetric key release uses),
    # never script_id (a definition id) which would generally miss the org-scoped lookup.
    manager = RealBrowserManager()
    state = _fake_browser_state()
    state.get_working_page = AsyncMock(return_value=MagicMock())
    state.get_or_create_page = AsyncMock()
    sessions = MagicMock()
    sessions.get_browser_state = AsyncMock(return_value=state)
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)
    result = await manager.get_or_create_for_script("scr_1", "session_1", "org_1")
    sessions.get_browser_state.assert_awaited_once_with("session_1", organization_id="org_1")
    assert result is state and manager.pages["scr_1"] is state


@pytest.mark.asyncio
async def test_get_or_create_for_script_fails_closed_without_org(monkeypatch: pytest.MonkeyPatch) -> None:
    # MF2: a session requested without an org identity fails closed — never look up under script_id nor
    # silently create an unrelated non-persistent browser.
    manager = RealBrowserManager()
    sessions = MagicMock()
    sessions.get_browser_state = AsyncMock()
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)
    created = AsyncMock()
    monkeypatch.setattr(manager, "_create_browser_state", created)
    with pytest.raises(MissingOrganizationForBrowserSession):
        await manager.get_or_create_for_script(script_id="scr_1", browser_session_id="session_1")
    sessions.get_browser_state.assert_not_awaited()
    created.assert_not_awaited()
    assert "scr_1" not in manager.pages


@pytest.mark.asyncio
async def test_get_or_create_for_script_fails_closed_on_cold_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # A requested browser session whose persistent state is cold/evicted (get_browser_state -> None) must
    # fail closed, NOT silently fall back to an unregistered local browser: cleanup would misclassify that
    # fallback as a reusable persistent session (browser_session_id truthy) and leak its context/driver.
    manager = RealBrowserManager()
    sessions = MagicMock()
    sessions.get_browser_state = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)
    fallback = _fake_browser_state()
    fallback.get_or_create_page = AsyncMock()
    created = AsyncMock(return_value=fallback)
    monkeypatch.setattr(manager, "_create_browser_state", created)

    with pytest.raises(MissingBrowserStateForBrowserSession):
        await manager.get_or_create_for_script(
            script_id="scr_1", browser_session_id="session_1", organization_id="org_1"
        )

    sessions.get_browser_state.assert_awaited_once_with("session_1", organization_id="org_1")
    created.assert_not_awaited()  # no orphan local browser created
    assert "scr_1" not in manager.pages


@pytest.mark.asyncio
async def test_cleanup_for_script_contains_drop_engine_owner_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # MF3: an ordinary _drop_engine_owner failure is contained so page pop/close cleanup is still attempted.
    manager = RealBrowserManager()
    state = _fake_browser_state()
    manager.pages["scr_1"] = state
    monkeypatch.setattr(manager, "_drop_engine_owner", AsyncMock(side_effect=RuntimeError("owner boom")))
    result = await manager.cleanup_for_script("scr_1")  # must not raise
    state.close.assert_awaited_once()
    assert "scr_1" not in manager.pages and result is state


@pytest.mark.asyncio
async def test_cleanup_for_script_reclaims_page_when_cancelled_mid_drop(_restore_resolver: None) -> None:
    # A terminal run cancelled while cleanup awaits the in-flight engine owner must STILL reclaim the page
    # (pop + close) before the cancellation surfaces — a cancelled terminal run cannot leak the browser it
    # was reclaiming — and the caller's own cancellation must stay native (re-raised, not swallowed).
    started = asyncio.Event()
    release = asyncio.Event()
    suppressed = asyncio.Event()

    async def gated_suppressing_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            suppressed.set()  # absorbed _drop_engine_owner's cancel; stay alive so cleanup parks on shield
            await release.wait()
        return _fake_selection("engine-1")

    browser_engine.set_browser_engine_resolver(gated_suppressing_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="scr_1", browser_source="local-browser")
    state = _fake_browser_state()

    first: asyncio.Future | None = None
    cleanup: asyncio.Future | None = None
    owner_task: asyncio.Task | None = None
    try:
        first = asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="scr_1", context=ctx))
        await started.wait()  # resolver #1 is running (the in-flight owner)
        owner_task = manager._engine_owners["scr_1"].task
        manager.pages["scr_1"] = state

        cleanup = asyncio.ensure_future(manager.cleanup_for_script("scr_1", close_browser_on_completion=False))
        await asyncio.wait_for(suppressed.wait(), timeout=5.0)  # cleanup is parked inside _drop_engine_owner

        cleanup.cancel()  # externally cancel the terminal cleanup itself
        with pytest.raises(asyncio.CancelledError):
            await cleanup  # our own cancellation propagates, not swallowed into a successful cleanup

        assert "scr_1" not in manager.pages  # page reclaimed despite the cancel
        state.close.assert_awaited_once()  # browser closed, not leaked
    finally:
        release.set()
        for task in (first, cleanup, owner_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in (first, cleanup, owner_task) if t is not None], return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_for_script_completes_release_when_cancelled_during_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A caller cancellation (shutdown/timeout) arriving while the browser close is in-flight must not skip
    # the persistent-session release: the whole page/trace/close/release reclamation is shielded so it runs
    # to completion, then the cancellation re-raises natively.
    manager = RealBrowserManager()
    in_close = asyncio.Event()
    finish_close = asyncio.Event()
    closed = asyncio.Event()

    async def slow_close(**_kwargs: object) -> None:
        in_close.set()
        await finish_close.wait()
        closed.set()

    state = _fake_browser_state()
    state.close = AsyncMock(side_effect=slow_close)
    manager.pages["scr_1"] = state
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock()
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)

    cleanup = asyncio.ensure_future(
        manager.cleanup_for_script("scr_1", browser_session_id="session_1", organization_id="org_1")
    )
    await in_close.wait()  # reclamation is parked inside close()
    cleanup.cancel()
    await asyncio.sleep(0)
    finish_close.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup  # cancellation stays native

    assert closed.is_set()  # close ran to completion despite the cancel
    sessions.release_browser_session.assert_awaited_once_with("session_1", organization_id="org_1")  # release too
    assert "scr_1" not in manager.pages


@pytest.mark.asyncio
async def test_cleanup_for_script_completes_when_cancelled_during_release(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cancellation arriving during the final persistent-session release must still let that release finish;
    # the shielded reclamation completes before the native cancellation propagates.
    manager = RealBrowserManager()
    in_release = asyncio.Event()
    finish_release = asyncio.Event()
    released = asyncio.Event()

    async def slow_release(*_a: object, **_k: object) -> None:
        in_release.set()
        await finish_release.wait()
        released.set()

    state = _fake_browser_state()
    manager.pages["scr_1"] = state
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(side_effect=slow_release)
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)

    cleanup = asyncio.ensure_future(
        manager.cleanup_for_script("scr_1", browser_session_id="session_1", organization_id="org_1")
    )
    await in_release.wait()
    cleanup.cancel()
    await asyncio.sleep(0)
    finish_release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert released.is_set()  # release ran to completion despite the cancel
    state.close.assert_awaited_once()  # close happened before the release
    assert "scr_1" not in manager.pages


@pytest.mark.asyncio
async def test_cleanup_for_script_survives_repeated_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Repeated caller cancellation (e.g. a shutdown that re-cancels) must not cancel the shielded
    # reclamation while it is being drained: close and release still complete, the task is not leaked, and
    # the FIRST native cancellation is preserved for re-raise.
    manager = RealBrowserManager()
    in_close = asyncio.Event()
    finish_close = asyncio.Event()
    closed = asyncio.Event()

    async def slow_close(**_kwargs: object) -> None:
        in_close.set()
        await finish_close.wait()
        closed.set()

    state = _fake_browser_state()
    state.close = AsyncMock(side_effect=slow_close)
    manager.pages["scr_1"] = state
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock()
    monkeypatch.setattr("skyvern.forge.app.PERSISTENT_SESSIONS_MANAGER", sessions)

    cleanup = asyncio.ensure_future(
        manager.cleanup_for_script("scr_1", browser_session_id="session_1", organization_id="org_1")
    )
    await in_close.wait()
    cleanup.cancel("first cancellation")  # outer shield raises; cleanup enters the drain
    await asyncio.sleep(0)
    cleanup.cancel("second cancellation")  # must NOT cancel the shielded reclamation
    await asyncio.sleep(0)
    finish_close.set()
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await cleanup

    assert exc_info.value.args == ("first cancellation",)
    assert closed.is_set()  # close ran to completion despite two cancels
    sessions.release_browser_session.assert_awaited_once_with("session_1", organization_id="org_1")
    assert "scr_1" not in manager.pages


@pytest.mark.asyncio
async def test_terminal_cleanup_with_waiters_ends_deterministically(_restore_resolver: None) -> None:
    # Cleanup while several waiters are blocked on the shared resolution must end deterministically:
    # every waiter is cancelled and no owner/task is left behind.
    gate = asyncio.Event()

    async def gated_resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        await gate.wait()
        return _fake_selection("engine-1")

    browser_engine.set_browser_engine_resolver(gated_resolver)
    manager = RealBrowserManager()
    ctx = BrowserEngineContext(workflow_run_id="wr_1", browser_source="local-browser")

    waiters = [
        asyncio.ensure_future(manager.get_or_resolve_engine_selection(run_key="wr_1", context=ctx)) for _ in range(3)
    ]
    await asyncio.sleep(0)  # all waiters attach to the shared owner task
    await manager._drop_engine_owner("wr_1")  # terminal cleanup with waiters still blocked
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    assert manager._engine_owners == {}
    gate.set()


@pytest.mark.asyncio
async def test_different_keys_resolve_concurrently(_restore_resolver: None) -> None:
    # Prove overlapping in-flight resolution: both keys enter the resolver before either is released.
    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    release = asyncio.Event()

    async def resolver(ctx: BrowserEngineContext) -> BrowserEngineSelection:
        key = ctx.workflow_run_id or ""
        started[key].set()
        await release.wait()
        return _fake_selection(f"engine-{key}")

    browser_engine.set_browser_engine_resolver(resolver)
    manager = RealBrowserManager()
    a = asyncio.ensure_future(
        manager.get_or_resolve_engine_selection(
            run_key="a", context=BrowserEngineContext(workflow_run_id="a", browser_source="local-browser")
        )
    )
    b = asyncio.ensure_future(
        manager.get_or_resolve_engine_selection(
            run_key="b", context=BrowserEngineContext(workflow_run_id="b", browser_source="local-browser")
        )
    )
    await asyncio.wait_for(asyncio.gather(started["a"].wait(), started["b"].wait()), timeout=1)
    release.set()
    ra, rb = await asyncio.gather(a, b)
    assert ra.name == "engine-a"
    assert rb.name == "engine-b"


def _coro(value: BrowserEngineSelection):
    async def _c(ctx: BrowserEngineContext | None = None) -> BrowserEngineSelection:
        return value

    return _c()
