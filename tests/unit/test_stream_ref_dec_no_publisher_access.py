"""Regression: ``stream_ref_dec`` must not touch publisher state on the
browser manager. Publisher lifecycle is driven through ``BrowserState.close()``;
``stream_ref_dec`` only closes/evicts the browser state.
"""

from __future__ import annotations

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
    registries.set_deferred_close_params(workflow_run_id, True)

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

    close_mock.assert_awaited_once_with(close_browser_on_completion=True)
    fake_manager.evict_page.assert_called_once_with(workflow_run_id)


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
