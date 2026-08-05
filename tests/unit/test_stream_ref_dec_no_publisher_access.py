"""Regression: ``stream_ref_dec`` must not touch publisher state on the
browser manager. Publisher lifecycle is driven through ``BrowserState.close()``;
``stream_ref_dec`` only closes/evicts the browser state.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from skyvern.forge.sdk.routes.streaming import registries


class _ExplodingPublisherStop:
    """Sentinel — any access to ``_stop_frame_publisher`` should fail the test."""

    def __get__(self, instance: object, owner: type | None = None) -> object:
        raise AssertionError(
            "stream_ref_dec must not read _stop_frame_publisher from app.BROWSER_MANAGER. "
            "Worker-side publisher lifecycle is driven by BrowserState.close(), not the "
            "API process."
        )


@pytest.mark.asyncio
async def test_stream_ref_dec_does_not_touch_publisher_on_deferred_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wr_no_api_publisher_access"
    registries.stream_ref_inc(workflow_run_id)
    assert registries.set_deferred_close_params(workflow_run_id, False, release_driver=False) is True

    close_mock = AsyncMock()
    fake_state = SimpleNamespace(close=close_mock)

    class _ManagerWithExplodingStop:
        # Reading this attribute MUST fail — the API code path is not allowed
        # to ask the worker manager about publishers.
        _stop_frame_publisher = _ExplodingPublisherStop()

        def __init__(self) -> None:
            self.pages: dict[str, object] = {workflow_run_id: fake_state}
            self.evict_page = Mock()

    fake_manager = _ManagerWithExplodingStop()
    fake_app = SimpleNamespace(BROWSER_MANAGER=fake_manager)

    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)

    # Pre-fix the descriptor raises on read; the post-fix code path doesn't
    # read it at all, so this must succeed.
    await registries.stream_ref_dec(workflow_run_id)

    close_mock.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    fake_manager.evict_page.assert_called_once_with(workflow_run_id)


def test_deferred_close_requires_an_active_stream() -> None:
    workflow_run_id = "wr_no_active_stream"

    assert registries.set_deferred_close_params(workflow_run_id, True) is False
    assert workflow_run_id not in registries._deferred_close_params


@pytest.mark.asyncio
async def test_stream_ref_dec_handles_missing_browser_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-process reality: API's ``BROWSER_MANAGER.pages`` is empty."""
    workflow_run_id = "wr_cross_process_missing_state"
    registries.stream_ref_inc(workflow_run_id)
    registries.set_deferred_close_params(workflow_run_id, True)

    evict_mock = Mock()
    # No _stop_frame_publisher attribute at all — the API code path must not
    # care whether the worker BrowserManager exposes one.
    fake_manager = SimpleNamespace(pages={}, evict_page=evict_mock)
    fake_app = SimpleNamespace(BROWSER_MANAGER=fake_manager)

    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)

    # Must not raise even though pages is empty (cross-process case).
    await registries.stream_ref_dec(workflow_run_id)
    evict_mock.assert_called_once_with(workflow_run_id)


def test_mark_closing_rejects_late_stream_attachment() -> None:
    workflow_run_id = "wr_closing"

    registries.mark_stream_closing(workflow_run_id)

    assert registries.try_stream_ref_inc(workflow_run_id) is False
    assert registries.stream_ref_active(workflow_run_id) is False


@pytest.mark.asyncio
async def test_last_decrement_before_finalizer_install_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wr_drained_before_install"
    browser_state = SimpleNamespace(close=AsyncMock())
    release = AsyncMock(return_value=True)
    fake_manager = SimpleNamespace(pages={workflow_run_id: browser_state}, evict_page=Mock())
    fake_app = SimpleNamespace(
        BROWSER_MANAGER=fake_manager,
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(release_browser_session=release),
    )
    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)

    assert registries.try_stream_ref_inc(workflow_run_id) is True
    registries.mark_stream_closing(workflow_run_id)
    await registries.stream_ref_dec(workflow_run_id)

    deferred = registries.set_deferred_close_params(
        workflow_run_id,
        False,
        release_driver=False,
        browser_session_id="pbs_drained",
        organization_id="org_drained",
        expected_runnable_id=workflow_run_id,
        expected_browser_state=browser_state,
    )

    assert deferred is False
    await registries.finalize_stream_teardown(workflow_run_id)
    browser_state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    release.assert_awaited_once_with(
        session_id="pbs_drained",
        organization_id="org_drained",
        expected_runnable_id=workflow_run_id,
        expected_runnable_generation_id=None,
        expected_browser_state=browser_state,
    )


@pytest.mark.asyncio
async def test_only_final_stream_decrement_releases_persistent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wr_two_streams"
    browser_state = SimpleNamespace(close=AsyncMock())
    release = AsyncMock(return_value=True)
    fake_manager = SimpleNamespace(pages={workflow_run_id: browser_state}, evict_page=Mock())
    fake_app = SimpleNamespace(
        BROWSER_MANAGER=fake_manager,
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(release_browser_session=release),
    )
    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)

    assert registries.try_stream_ref_inc(workflow_run_id) is True
    assert registries.try_stream_ref_inc(workflow_run_id) is True
    registries.mark_stream_closing(workflow_run_id)
    assert (
        registries.set_deferred_close_params(
            workflow_run_id,
            False,
            release_driver=False,
            browser_session_id="pbs_two",
            organization_id="org_two",
            expected_runnable_id=workflow_run_id,
            expected_browser_state=browser_state,
        )
        is True
    )

    await registries.stream_ref_dec(workflow_run_id)
    release.assert_not_awaited()
    await registries.stream_ref_dec(workflow_run_id)

    release.assert_awaited_once()
    browser_state.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_finalizer_cannot_run_before_streams_are_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wr_not_drained"
    browser_state = SimpleNamespace(close=AsyncMock())
    release = AsyncMock(return_value=True)
    fake_manager = SimpleNamespace(pages={workflow_run_id: browser_state}, evict_page=Mock())
    fake_app = SimpleNamespace(
        BROWSER_MANAGER=fake_manager,
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(release_browser_session=release),
    )
    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)
    assert registries.try_stream_ref_inc(workflow_run_id) is True
    registries.mark_stream_closing(workflow_run_id)
    assert (
        registries.set_deferred_close_params(
            workflow_run_id,
            False,
            release_driver=False,
            browser_session_id="pbs_not_drained",
            organization_id="org_not_drained",
            expected_runnable_id=workflow_run_id,
            expected_browser_state=browser_state,
        )
        is True
    )

    await registries.finalize_stream_teardown(workflow_run_id)

    browser_state.close.assert_not_awaited()
    release.assert_not_awaited()
    await registries.stream_ref_dec(workflow_run_id)
    release.assert_awaited_once_with(
        session_id="pbs_not_drained",
        organization_id="org_not_drained",
        expected_runnable_id=workflow_run_id,
        expected_runnable_generation_id=None,
        expected_browser_state=browser_state,
    )


@pytest.mark.asyncio
async def test_concurrent_ready_finalizers_run_owner_release_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wr_exactly_once"
    finalizer_entered = asyncio.Event()
    allow_finalizer = asyncio.Event()

    async def slow_close(**_: object) -> None:
        finalizer_entered.set()
        await allow_finalizer.wait()

    browser_state = SimpleNamespace(close=AsyncMock(side_effect=slow_close))
    release = AsyncMock(return_value=True)
    fake_manager = SimpleNamespace(pages={workflow_run_id: browser_state}, evict_page=Mock())
    fake_app = SimpleNamespace(
        BROWSER_MANAGER=fake_manager,
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(release_browser_session=release),
    )
    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)
    registries.mark_stream_closing(workflow_run_id)
    assert (
        registries.set_deferred_close_params(
            workflow_run_id,
            False,
            release_driver=False,
            browser_session_id="pbs_once",
            organization_id="org_once",
            expected_runnable_id=workflow_run_id,
            expected_browser_state=browser_state,
        )
        is False
    )

    first = asyncio.create_task(registries.finalize_stream_teardown(workflow_run_id))
    await finalizer_entered.wait()
    second = asyncio.create_task(registries.finalize_stream_teardown(workflow_run_id))
    await asyncio.sleep(0)
    allow_finalizer.set()
    await asyncio.gather(first, second)

    browser_state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    release.assert_awaited_once_with(
        session_id="pbs_once",
        organization_id="org_once",
        expected_runnable_id=workflow_run_id,
        expected_runnable_generation_id=None,
        expected_browser_state=browser_state,
    )


@pytest.mark.asyncio
async def test_failed_owner_release_retries_automatically_before_reaper_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wr_retry_finalizer"
    browser_state = SimpleNamespace(close=AsyncMock())
    release = AsyncMock(side_effect=[False, True])
    fake_manager = SimpleNamespace(pages={workflow_run_id: browser_state}, evict_page=Mock())
    fake_app = SimpleNamespace(
        BROWSER_MANAGER=fake_manager,
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(release_browser_session=release),
    )
    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)
    monkeypatch.setattr(registries, "STREAM_FINALIZER_RELEASE_RETRY_DELAY_SECONDS", 0)
    registries.mark_stream_closing(workflow_run_id)
    registries.set_deferred_close_params(
        workflow_run_id,
        False,
        release_driver=False,
        browser_session_id="pbs_retry",
        organization_id="org_retry",
        expected_runnable_id=workflow_run_id,
        expected_browser_state=browser_state,
    )

    await registries.finalize_stream_teardown(workflow_run_id)

    assert release.await_count == 2
    assert workflow_run_id not in registries._closing_streams


@pytest.mark.asyncio
async def test_exhausted_owner_release_still_clears_the_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream_ref_dec pops the refcount before calling the finalizer, so nothing can re-enter it.
    Keeping the tombstone would reject every later attach for this run forever."""
    workflow_run_id = "wr_exhausted_finalizer"
    browser_state = SimpleNamespace(close=AsyncMock())
    release = AsyncMock(return_value=False)
    fake_manager = SimpleNamespace(pages={workflow_run_id: browser_state}, evict_page=Mock())
    fake_app = SimpleNamespace(
        BROWSER_MANAGER=fake_manager,
        PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(release_browser_session=release),
    )
    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)
    monkeypatch.setattr(registries, "STREAM_FINALIZER_RELEASE_RETRY_DELAY_SECONDS", 0)
    registries.mark_stream_closing(workflow_run_id)
    registries.set_deferred_close_params(
        workflow_run_id,
        False,
        release_driver=False,
        browser_session_id="pbs_exhausted",
        organization_id="org_exhausted",
        expected_runnable_id=workflow_run_id,
        expected_browser_state=browser_state,
    )

    await registries.finalize_stream_teardown(workflow_run_id)

    assert release.await_count == registries.STREAM_FINALIZER_RELEASE_MAX_ATTEMPTS
    assert workflow_run_id not in registries._closing_streams
    assert workflow_run_id not in registries._deferred_close_params
