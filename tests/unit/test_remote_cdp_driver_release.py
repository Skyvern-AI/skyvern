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

from skyvern.webeye import browser_engine
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_engine import (
    BrowserEngineContext,
    BrowserEngineMetadata,
    BrowserEngineSelection,
    BrowserSourceNotSupportedByEngine,
)
from skyvern.webeye.real_browser_manager import RealBrowserManager, _EngineSelectionOwner, canonical_run_key
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


def _coro(value: BrowserEngineSelection):
    async def _c(ctx: BrowserEngineContext | None = None) -> BrowserEngineSelection:
        return value

    return _c()
