"""``_start_frame_publisher`` ties the publisher to ``BrowserState.close()``.

The publisher follows the browser state regardless of who closes it, so the
API-side ``stream_ref_dec`` does not need to reach into publisher internals.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.webeye import real_browser_manager as manager_module
from skyvern.webeye.real_browser_manager import RealBrowserManager


class _RecordingPublisher:
    def __init__(self, *, browser_state: Any, stream_key: str, organization_id: str) -> None:
        self.browser_state = browser_state
        self.stream_key = stream_key
        self.organization_id = organization_id
        self.stop_called = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stop_called += 1


class _FakeBrowserState:
    """Stand-in for ``RealBrowserState`` with the marker stamped on."""

    def __init__(self) -> None:
        self.browser_artifacts = SimpleNamespace(needs_cdp_frame_publisher=True)
        self._on_close_callbacks: list[Callable[[], Awaitable[None]]] = []

    def add_on_close(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_close_callbacks.append(callback)

    async def fire_close(self) -> None:
        for cb in self._on_close_callbacks:
            await cb()
        self._on_close_callbacks.clear()


@pytest.fixture
def patched_publisher_factory(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingPublisher]:
    created: list[_RecordingPublisher] = []

    def _factory(**kwargs: Any) -> _RecordingPublisher:
        pub = _RecordingPublisher(**kwargs)
        created.append(pub)
        return pub

    monkeypatch.setattr(manager_module, "CDPFramePublisher", _factory)
    return created


@pytest.mark.asyncio
async def test_publisher_stops_when_browser_state_closes(
    patched_publisher_factory: list[_RecordingPublisher],
) -> None:
    manager = RealBrowserManager()
    browser_state = _FakeBrowserState()

    await manager._start_frame_publisher(
        browser_state=browser_state,
        workflow_run_id="wr_self_stop",
        organization_id="o_1",
    )
    assert manager._frame_publishers["wr_self_stop.png"] is patched_publisher_factory[0]

    # Simulate BrowserState.close() invoking its registered callbacks.
    await browser_state.fire_close()

    assert patched_publisher_factory[0].stop_called == 1
    # Registry is cleared so a re-start later would not see a stale entry.
    assert "wr_self_stop.png" not in manager._frame_publishers


@pytest.mark.asyncio
async def test_double_close_callback_safe(
    patched_publisher_factory: list[_RecordingPublisher],
) -> None:
    """Worker cleanup may stop the publisher explicitly AND BrowserState.close
    may fire the registered callback. The second stop must be a no-op."""
    manager = RealBrowserManager()
    browser_state = _FakeBrowserState()

    await manager._start_frame_publisher(
        browser_state=browser_state,
        workflow_run_id="wr_double",
        organization_id="o_1",
    )

    # Explicit worker-side stop (e.g. cleanup_for_workflow_run synchronous branch)
    await manager._stop_frame_publisher(workflow_run_id="wr_double")
    assert patched_publisher_factory[0].stop_called == 1
    assert "wr_double.png" not in manager._frame_publishers

    # Then the registered on-close callback runs (e.g. via browser_state.close()
    # later). The pop returns None, so no second stop is dispatched.
    await browser_state.fire_close()
    assert patched_publisher_factory[0].stop_called == 1
