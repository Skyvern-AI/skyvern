"""
Tests for RealBrowserManager cache behavior (regression coverage for PR #9020).

PR #9020 introduced a regression where the self.pages cache check was gated
behind `if not browser_session_id:`, causing PBS workflow runs to skip the cache
on every call and re-invoke navigate_to_url() on every step.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import loop_iteration_key
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.real_browser_manager import RealBrowserManager


def make_workflow_run(
    workflow_run_id: str,
    parent_workflow_run_id: str | None = None,
    organization_id: str = "org_test",
    browser_profile_id: str | None = None,
) -> MagicMock:
    wfr = MagicMock()
    wfr.workflow_run_id = workflow_run_id
    wfr.parent_workflow_run_id = parent_workflow_run_id
    wfr.organization_id = organization_id
    wfr.browser_profile_id = browser_profile_id
    wfr.proxy_location = None
    wfr.extra_http_headers = None
    wfr.browser_address = None
    return wfr


@pytest.mark.asyncio
async def test_pbs_workflow_run_cache_hit_on_second_call() -> None:
    """PBS runs must hit the cache on subsequent calls and NOT re-enter the PBS branch."""
    manager = RealBrowserManager()
    cached_state = MagicMock()
    manager.pages["wfr_child"] = cached_state

    workflow_run = make_workflow_run("wfr_child")
    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="bs_123",
        )
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_not_called()

    assert result is cached_state


@pytest.mark.asyncio
async def test_pbs_workflow_run_does_not_inherit_parent_browser() -> None:
    """Child PBS runs must NOT inherit the parent's browser on the first call."""
    manager = RealBrowserManager()
    parent_state = MagicMock()
    manager.pages["wfr_parent"] = parent_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")

    pbs_state = MagicMock()
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="bs_123",
        )

    # Must use the PBS session, not the parent's browser
    assert result is pbs_state
    assert result is not parent_state


@pytest.mark.asyncio
async def test_pbs_workflow_run_returns_own_cache_not_parent() -> None:
    """When both child and parent are cached, PBS must return the child's own entry."""
    manager = RealBrowserManager()
    child_state = MagicMock()
    manager.pages["wfr_child"] = child_state
    manager.pages["wfr_parent"] = MagicMock()

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")
    result = await manager.get_or_create_for_workflow_run(
        workflow_run=workflow_run,
        url="https://example.com",
        browser_session_id="bs_123",
    )

    assert result is child_state


@pytest.mark.asyncio
async def test_non_pbs_workflow_run_cache_hit_on_second_call() -> None:
    """Non-PBS runs must also hit the early cache check on subsequent calls."""
    manager = RealBrowserManager()
    cached_state = MagicMock()
    manager.pages["wfr_child"] = cached_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")
    result = await manager.get_or_create_for_workflow_run(
        workflow_run=workflow_run,
        url=None,
        browser_session_id=None,
    )

    assert result is cached_state


@pytest.mark.asyncio
async def test_non_pbs_workflow_run_inherits_parent_browser() -> None:
    """Non-PBS child runs must still inherit the parent's browser when no browser_session_id."""
    manager = RealBrowserManager()
    parent_state = MagicMock()
    manager.pages["wfr_parent"] = parent_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")

    result = await manager.get_or_create_for_workflow_run(
        workflow_run=workflow_run,
        url=None,
        browser_session_id=None,
    )

    assert result is parent_state
    # Both entries should be synced
    assert manager.pages["wfr_child"] is parent_state
    assert manager.pages["wfr_parent"] is parent_state


@pytest.mark.asyncio
async def test_parallel_iteration_browser_returned_from_context() -> None:
    """When SkyvernContext has an iteration browser_session_id, get_or_create_for_workflow_run
    must return the pre-created iteration browser instead of creating a new one."""
    manager = RealBrowserManager()
    iteration_state = MagicMock()
    iteration_state.get_working_page = AsyncMock(return_value=MagicMock(url="https://example.com"))
    manager.pages["wr_abc__iter_0"] = iteration_state

    workflow_run = make_workflow_run("wr_abc")

    # Set up SkyvernContext with iteration browser_session_id
    ctx = SkyvernContext(browser_session_id="wr_abc__iter_0")
    skyvern_context.set(ctx)
    try:
        with patch("skyvern.webeye.real_browser_manager.app"):
            result = await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
            )
        assert result is iteration_state
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_parallel_iteration_browser_not_used_without_context() -> None:
    """Without a parallel iteration context, normal lookup applies even if
    iteration keys exist in pages."""
    manager = RealBrowserManager()
    iteration_state = MagicMock()
    normal_state = MagicMock()
    manager.pages["wr_abc__iter_0"] = iteration_state
    manager.pages["wr_abc"] = normal_state

    workflow_run = make_workflow_run("wr_abc")

    # No SkyvernContext set (or context without iteration marker)
    skyvern_context.reset()
    with patch("skyvern.webeye.real_browser_manager.app"):
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
        )
    assert result is normal_state


@pytest.mark.asyncio
async def test_parallel_iteration_different_iterations_get_different_browsers() -> None:
    """Each parallel iteration should get its own isolated browser state."""
    manager = RealBrowserManager()
    iter0_state = MagicMock()
    iter0_state.get_working_page = AsyncMock(return_value=MagicMock(url="https://example.com"))
    iter1_state = MagicMock()
    iter1_state.get_working_page = AsyncMock(return_value=MagicMock(url="https://example.com"))
    manager.pages["wr_abc__iter_0"] = iter0_state
    manager.pages["wr_abc__iter_1"] = iter1_state

    workflow_run = make_workflow_run("wr_abc")

    # Iteration 0
    ctx0 = SkyvernContext(browser_session_id="wr_abc__iter_0")
    skyvern_context.set(ctx0)
    try:
        with patch("skyvern.webeye.real_browser_manager.app"):
            result0 = await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
            )
    finally:
        skyvern_context.reset()

    # Iteration 1
    ctx1 = SkyvernContext(browser_session_id="wr_abc__iter_1")
    skyvern_context.set(ctx1)
    try:
        with patch("skyvern.webeye.real_browser_manager.app"):
            result1 = await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
            )
    finally:
        skyvern_context.reset()

    assert result0 is iter0_state
    assert result1 is iter1_state
    assert result0 is not result1


# ---------------------------------------------------------------------------
# Parallel-loop browser launch must not be serialized on a lock.
# ---------------------------------------------------------------------------


def _make_fake_browser_state() -> MagicMock:
    state = MagicMock()
    state.get_or_create_page = AsyncMock(return_value=None)
    state.close = AsyncMock(return_value=None)
    return state


@pytest.mark.asyncio
async def test_loop_iteration_launches_overlap() -> None:
    """Concurrent get_or_create_for_loop_iteration calls with distinct keys
    must overlap in wall-clock time. If launches serialize under a lock,
    N * per_launch is observed; this asserts < 2 * per_launch for 4 calls.
    """
    manager = RealBrowserManager()
    per_launch = 0.3
    call_count = 0

    async def fake_create(**_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(per_launch)
        return _make_fake_browser_state()

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=fake_create):
        start = time.perf_counter()
        results = await asyncio.gather(
            *(
                manager.get_or_create_for_loop_iteration(
                    workflow_run_id="wr_overlap",
                    loop_idx=i,
                    organization_id="org_test",
                )
                for i in range(4)
            )
        )
        elapsed = time.perf_counter() - start

    assert call_count == 4, f"expected 4 launches, got {call_count}"
    assert len(manager.pages) == 4
    # Serialized would be ~1.2s; parallel ~0.3s. 3x gives jitter slack for
    # slow CI runners (GitHub Actions can stall 500ms+ on GC). call_count
    # already proves correctness; this bound is a secondary signal.
    assert elapsed < per_launch * 3, (
        f"concurrent launches did not overlap: elapsed={elapsed:.3f}s "
        f"(per_launch={per_launch}s, expected < {per_launch * 3}s)"
    )
    # Every returned state must be a distinct object.
    assert len({id(r) for r in results}) == 4


@pytest.mark.asyncio
async def test_loop_iteration_single_flight_same_key() -> None:
    """Concurrent callers for the same key must collapse to a single
    underlying browser launch (single-flight)."""
    manager = RealBrowserManager()
    call_count = 0
    created_state = _make_fake_browser_state()

    async def fake_create(**_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)
        return created_state

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=fake_create):
        results = await asyncio.gather(
            *(
                manager.get_or_create_for_loop_iteration(
                    workflow_run_id="wr_single_flight",
                    loop_idx=7,
                    organization_id="org_test",
                )
                for _ in range(4)
            )
        )

    assert call_count == 1
    assert len(manager.pages) == 1
    assert all(r is created_state for r in results)


@pytest.mark.asyncio
async def test_loop_iteration_owner_cancellation_does_not_orphan_inflight() -> None:
    """If the launch-owning task is cancelled mid-launch, subsequent callers
    for the same key must not await a stale in-flight future forever — the
    in-flight entry must be cleared so the next caller starts a fresh
    launch."""
    manager = RealBrowserManager()
    launch_started = asyncio.Event()
    call_count = 0

    async def hanging_create(**_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        launch_started.set()
        # Hang until cancelled.
        await asyncio.sleep(10)
        return _make_fake_browser_state()  # pragma: no cover

    async def fast_create(**_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return _make_fake_browser_state()

    # First call: owner, gets cancelled while _create_browser_state hangs.
    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=hanging_create):
        owner_task = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_cancel",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await launch_started.wait()
        owner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner_task

    # In-flight entry must be cleared so a retry is not stuck waiting on it.
    assert "wr_cancel__iter_0" not in manager._loop_iteration_inflight
    assert "wr_cancel__iter_0" not in manager.pages

    # Retry succeeds with a fresh launch.
    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=fast_create):
        result = await manager.get_or_create_for_loop_iteration(
            workflow_run_id="wr_cancel",
            loop_idx=0,
            organization_id="org_test",
        )

    assert result is not None
    assert call_count == 2
    assert "wr_cancel__iter_0" in manager.pages


@pytest.mark.asyncio
async def test_loop_iteration_waiter_cancellation_does_not_cancel_owner() -> None:
    """Cancelling one waiter on a single-flight key must not cancel the
    shared launch future — otherwise the owner and every other waiter
    would fan out into CancelledError."""
    manager = RealBrowserManager()
    launch_started = asyncio.Event()
    call_count = 0
    created_state = _make_fake_browser_state()

    async def slow_create(**_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        launch_started.set()
        await asyncio.sleep(0.2)
        return created_state

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=slow_create):
        owner = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_waiter_cancel",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await launch_started.wait()

        waiter_to_cancel = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_waiter_cancel",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        surviving_waiter = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_waiter_cancel",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        # Let the waiters reach their shielded await.
        await asyncio.sleep(0)
        waiter_to_cancel.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_to_cancel

        owner_result = await owner
        surviving_result = await surviving_waiter

    assert call_count == 1
    assert owner_result is created_state
    assert surviving_result is created_state


@pytest.mark.asyncio
async def test_cleanup_during_launch_discards_orphan_state() -> None:
    """If cleanup_loop_iterations runs while a launch is still in-flight
    for the same key, the owner must close the browser state it created
    instead of publishing it into self.pages — otherwise an orphaned
    Playwright context is leaked."""
    manager = RealBrowserManager()
    launch_started = asyncio.Event()
    cleanup_done = asyncio.Event()
    created_state = _make_fake_browser_state()

    async def gated_create(**_kwargs: object) -> MagicMock:
        launch_started.set()
        await cleanup_done.wait()
        return created_state

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=gated_create):
        owner = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_race",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await launch_started.wait()
        # Cleanup runs mid-launch.
        await manager.cleanup_loop_iterations(
            workflow_run_id="wr_race",
            loop_indices=[0],
            organization_id="org_test",
        )
        cleanup_done.set()
        with pytest.raises(RuntimeError, match="cleaned up during launch"):
            await owner

    assert "wr_race__iter_0" not in manager.pages
    assert "wr_race__iter_0" not in manager._loop_iteration_inflight
    created_state.close.assert_awaited()


@pytest.mark.asyncio
async def test_cleanup_during_launch_does_not_disturb_replacement_owner() -> None:
    """After cleanup cancels owner A's in-flight future, a replacement
    owner B may register a new future for the same key before owner A
    finishes unwinding. Owner A must not pop B's future (Codex P1)."""
    manager = RealBrowserManager()
    owner_a_started = asyncio.Event()
    owner_a_can_finish = asyncio.Event()
    states: list[MagicMock] = []

    async def gated_create(**_kwargs: object) -> MagicMock:
        state = _make_fake_browser_state()
        states.append(state)
        if len(states) == 1:
            owner_a_started.set()
            await owner_a_can_finish.wait()
        return state

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=gated_create):
        owner_a = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_replace",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await owner_a_started.wait()

        # Cleanup cancels owner A's in-flight future.
        await manager.cleanup_loop_iterations(
            workflow_run_id="wr_replace",
            loop_indices=[0],
            organization_id="org_test",
        )

        # Owner B registers a brand-new future for the same key.
        owner_b = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_replace",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        # Let owner B reach its await on _create_browser_state (second call).
        await asyncio.sleep(0)

        # Now let owner A finish unwinding. It must not disturb owner B's
        # inflight entry.
        owner_a_can_finish.set()
        with pytest.raises(RuntimeError, match="cleaned up during launch"):
            await owner_a

        result_b = await owner_b

    assert result_b is states[1]
    assert manager.pages["wr_replace__iter_0"] is states[1]
    assert "wr_replace__iter_0" not in manager._loop_iteration_inflight
    # Owner A's orphaned state was closed; owner B's state is intact.
    states[0].close.assert_awaited()
    states[1].close.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_failure_does_not_disturb_replacement_inflight() -> None:
    """Owner A's failure path must identity-check before popping inflight.
    If cleanup cancelled A and owner B registered a replacement future,
    A's failure must not disturb B's entry (Codex P1)."""
    manager = RealBrowserManager()
    owner_a_started = asyncio.Event()
    owner_a_can_finish = asyncio.Event()
    states: list[MagicMock] = []

    class LaunchFailure(RuntimeError):
        pass

    async def gated_create(**_kwargs: object) -> MagicMock:
        if not states:
            states.append(object())  # sentinel for "A ran"
            owner_a_started.set()
            await owner_a_can_finish.wait()
            raise LaunchFailure("A failed")
        state = _make_fake_browser_state()
        states.append(state)
        return state

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=gated_create):
        owner_a = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_fail_race",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await owner_a_started.wait()

        await manager.cleanup_loop_iterations(
            workflow_run_id="wr_fail_race",
            loop_indices=[0],
            organization_id="org_test",
        )

        owner_b = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_fail_race",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await asyncio.sleep(0)

        owner_a_can_finish.set()
        # Owner A raises — it was cancelled by cleanup, so CancelledError
        # propagates first (cancellation takes precedence over the
        # subsequent LaunchFailure); either way it must not touch B.
        with pytest.raises(BaseException):
            await owner_a

        result_b = await owner_b

    assert result_b is states[1]
    assert manager.pages["wr_fail_race__iter_0"] is states[1]
    assert "wr_fail_race__iter_0" not in manager._loop_iteration_inflight
    states[1].close.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_during_launch_does_not_orphan_state() -> None:
    """RealBrowserManager.close() while an owner is mid-launch must
    cancel the owner's in-flight future and cause the owner to close
    its own newly-created state instead of publishing it into
    self.pages after close() has already reset the dict."""
    manager = RealBrowserManager()
    launch_started = asyncio.Event()
    close_done = asyncio.Event()
    created_state = _make_fake_browser_state()

    async def gated_create(**_kwargs: object) -> MagicMock:
        launch_started.set()
        await close_done.wait()
        return created_state

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=gated_create):
        owner = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_close_race",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await launch_started.wait()

        await manager.close()
        close_done.set()
        with pytest.raises(RuntimeError, match="cleaned up during launch"):
            await owner

    assert "wr_close_race__iter_0" not in manager.pages
    assert "wr_close_race__iter_0" not in manager._loop_iteration_inflight
    created_state.close.assert_awaited()


@pytest.mark.asyncio
async def test_cleanup_during_launch_surfaces_exception_to_waiters() -> None:
    """Codex P2 regression: cleanup_loop_iterations must surface a regular
    Exception (not CancelledError) to same-key waiters. CancelledError is a
    BaseException and would bypass `_execute_single_iteration_parallel`'s
    `except Exception` handler and `_execute_loop_parallel`'s
    `isinstance(result, Exception)` dispatch, crashing the batch unpack."""
    manager = RealBrowserManager()
    owner_started = asyncio.Event()
    cleanup_done = asyncio.Event()
    created_state = _make_fake_browser_state()

    async def gated_create(**_kwargs: object) -> MagicMock:
        owner_started.set()
        await cleanup_done.wait()
        return created_state

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=gated_create):
        owner = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_waiter_exc",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await owner_started.wait()

        # Attach waiters AFTER the in-flight future is registered but
        # BEFORE cleanup runs, so they have a shielded reference that
        # cleanup's set_exception must reach.
        waiters = [
            asyncio.create_task(
                manager.get_or_create_for_loop_iteration(
                    workflow_run_id="wr_waiter_exc",
                    loop_idx=0,
                    organization_id="org_test",
                )
            )
            for _ in range(2)
        ]
        # Yield so waiters reach `await asyncio.shield(inflight_future)`.
        await asyncio.sleep(0)

        await manager.cleanup_loop_iterations(
            workflow_run_id="wr_waiter_exc",
            loop_indices=[0],
            organization_id="org_test",
        )
        cleanup_done.set()

        for waiter in waiters:
            with pytest.raises(Exception) as excinfo:
                await waiter
            # Critically, not a CancelledError — that would bypass the
            # batch handler and crash the success-unpack path.
            assert not isinstance(excinfo.value, asyncio.CancelledError), (
                "waiter must receive a regular Exception, not CancelledError"
            )
            assert "cleaned up during launch" in str(excinfo.value)

        with pytest.raises(RuntimeError, match="cleaned up during launch"):
            await owner

    assert "wr_waiter_exc__iter_0" not in manager.pages
    assert "wr_waiter_exc__iter_0" not in manager._loop_iteration_inflight


@pytest.mark.asyncio
async def test_owner_cancellation_surfaces_exception_to_waiters() -> None:
    """If the launch-owning task is cancelled while same-key waiters are
    attached, waiters must receive a regular Exception (not CancelledError)
    so the batch driver's `isinstance(result, Exception)` dispatch catches
    them instead of the success-unpack path crashing on a BaseException."""
    manager = RealBrowserManager()
    owner_started = asyncio.Event()

    async def hanging_create(**_kwargs: object) -> MagicMock:
        owner_started.set()
        await asyncio.sleep(10)
        return _make_fake_browser_state()  # pragma: no cover

    with patch.object(RealBrowserManager, "_create_browser_state", side_effect=hanging_create):
        owner = asyncio.create_task(
            manager.get_or_create_for_loop_iteration(
                workflow_run_id="wr_owner_cancel_waiters",
                loop_idx=0,
                organization_id="org_test",
            )
        )
        await owner_started.wait()

        waiters = [
            asyncio.create_task(
                manager.get_or_create_for_loop_iteration(
                    workflow_run_id="wr_owner_cancel_waiters",
                    loop_idx=0,
                    organization_id="org_test",
                )
            )
            for _ in range(2)
        ]
        # Let waiters reach their shielded await.
        await asyncio.sleep(0)

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

        for waiter in waiters:
            with pytest.raises(Exception) as excinfo:
                await waiter
            assert not isinstance(excinfo.value, asyncio.CancelledError), (
                "waiter must receive a regular Exception when owner is cancelled"
            )
            assert "launch cancelled" in str(excinfo.value)

    assert "wr_owner_cancel_waiters__iter_0" not in manager._loop_iteration_inflight
    assert "wr_owner_cancel_waiters__iter_0" not in manager.pages


@pytest.mark.asyncio
async def test_cleanup_loop_iterations_runs_close_in_parallel() -> None:
    """cleanup_loop_iterations must close browser contexts in parallel.
    Sequential close of 4 entries at 0.2s each would take ~0.8s; parallel
    should finish in ~0.2s. 0.4s ceiling gives CI slack."""
    manager = RealBrowserManager()
    per_close = 0.2

    async def slow_close() -> None:
        await asyncio.sleep(per_close)

    loop_indices = [0, 1, 2, 3]
    for idx in loop_indices:
        state = _make_fake_browser_state()
        state.close = AsyncMock(side_effect=slow_close)
        manager.pages[loop_iteration_key("wr_cleanup", idx)] = state

    start = time.perf_counter()
    await manager.cleanup_loop_iterations(
        workflow_run_id="wr_cleanup",
        loop_indices=loop_indices,
        organization_id="org_test",
    )
    elapsed = time.perf_counter() - start

    assert len(manager.pages) == 0
    # 3x for CI jitter slack; len(manager.pages) == 0 is the real correctness check.
    assert elapsed < per_close * 3, (
        f"cleanup did not run closes in parallel: elapsed={elapsed:.3f}s "
        f"(per_close={per_close}s, expected < {per_close * 3}s)"
    )
