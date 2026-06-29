"""Wire-up tests for ``RealBrowserManager``'s CDP frame publisher helpers.

Exercises ``_start_frame_publisher`` / ``_stop_frame_publisher`` and their
cleanup interaction with ``self._frame_publishers``. The publisher loop itself
is covered by ``test_worker_cdp_frame_publisher.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye import real_browser_manager as manager_module
from skyvern.webeye.real_browser_manager import RealBrowserManager, _resolve_stream_key


def test_resolve_stream_key_prefers_workflow_run() -> None:
    assert _resolve_stream_key(workflow_run_id="wr_1", task_id="tsk_2") == "wr_1.png"


def test_resolve_stream_key_falls_back_to_task() -> None:
    assert _resolve_stream_key(workflow_run_id=None, task_id="tsk_2") == "tsk_2.png"


def test_resolve_stream_key_returns_none_when_no_identifier() -> None:
    assert _resolve_stream_key(workflow_run_id=None, task_id=None) is None


class _RecordingPublisher:
    """Stand-in for ``CDPFramePublisher`` that tracks lifecycle calls."""

    def __init__(
        self,
        *,
        browser_state: object,
        stream_key: str,
        organization_id: str,
    ) -> None:
        self.browser_state = browser_state
        self.stream_key = stream_key
        self.organization_id = organization_id
        self.start_called = 0
        self.stop_called = 0

    async def start(self) -> None:
        self.start_called += 1

    async def stop(self) -> None:
        self.stop_called += 1


def _marked_browser_state(*, needs_publisher: bool = True) -> SimpleNamespace:
    """Fake BrowserState carrying the marker that the factory stamps."""
    return SimpleNamespace(
        browser_artifacts=SimpleNamespace(needs_cdp_frame_publisher=needs_publisher),
        add_on_close=lambda _cb: None,
    )


@pytest.fixture
def patched_publisher(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingPublisher]:
    created: list[_RecordingPublisher] = []

    def _factory(**kwargs: object) -> _RecordingPublisher:
        pub = _RecordingPublisher(**kwargs)  # type: ignore[arg-type]
        created.append(pub)
        return pub

    monkeypatch.setattr(manager_module, "CDPFramePublisher", _factory)
    return created


@pytest.mark.asyncio
async def test_start_frame_publisher_gated_off_when_state_unmarked(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    """The marker is the gate: an unmarked BrowserState produces no publisher
    even when stream_key + organization_id are valid."""
    manager = RealBrowserManager()
    await manager._start_frame_publisher(
        browser_state=_marked_browser_state(needs_publisher=False),
        workflow_run_id="wr_42",
        organization_id="o_1",
    )
    assert patched_publisher == []
    assert manager._frame_publishers == {}


@pytest.mark.asyncio
async def test_start_frame_publisher_creates_publisher_with_workflow_key(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    manager = RealBrowserManager()
    browser_state = _marked_browser_state()

    await manager._start_frame_publisher(
        browser_state=browser_state,
        workflow_run_id="wr_42",
        task_id="tsk_99",
        organization_id="o_1",
    )

    assert len(patched_publisher) == 1
    pub = patched_publisher[0]
    assert pub.stream_key == "wr_42.png"
    assert pub.organization_id == "o_1"
    assert pub.start_called == 1
    assert manager._frame_publishers["wr_42.png"] is pub


@pytest.mark.asyncio
async def test_start_frame_publisher_uses_task_key_when_no_workflow(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    manager = RealBrowserManager()
    await manager._start_frame_publisher(
        browser_state=_marked_browser_state(),
        task_id="tsk_99",
        organization_id="o_1",
    )

    assert patched_publisher[0].stream_key == "tsk_99.png"


@pytest.mark.asyncio
async def test_start_frame_publisher_is_noop_when_already_running(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    manager = RealBrowserManager()

    for _ in range(3):
        await manager._start_frame_publisher(
            browser_state=_marked_browser_state(),
            workflow_run_id="wr_42",
            organization_id="o_1",
        )

    assert len(patched_publisher) == 1
    assert patched_publisher[0].start_called == 1


@pytest.mark.asyncio
async def test_start_frame_publisher_requires_organization_id(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    manager = RealBrowserManager()
    await manager._start_frame_publisher(
        browser_state=_marked_browser_state(),
        workflow_run_id="wr_42",
        organization_id=None,
    )
    assert patched_publisher == []
    assert manager._frame_publishers == {}


@pytest.mark.asyncio
async def test_start_frame_publisher_swallows_start_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_RecordingPublisher] = []

    class _ExplodingPublisher(_RecordingPublisher):
        async def start(self) -> None:  # noqa: D401
            raise RuntimeError("kaboom")

    def _factory(**kwargs: object) -> _ExplodingPublisher:
        pub = _ExplodingPublisher(**kwargs)  # type: ignore[arg-type]
        created.append(pub)
        return pub

    monkeypatch.setattr(manager_module, "CDPFramePublisher", _factory)

    manager = RealBrowserManager()
    # Must not raise: livestream is best-effort.
    await manager._start_frame_publisher(
        browser_state=_marked_browser_state(),
        workflow_run_id="wr_42",
        organization_id="o_1",
    )
    # The factory must have been invoked — otherwise this test would silently
    # exit at the feature gate without exercising the exception-handling path.
    assert len(created) == 1
    assert manager._frame_publishers == {}


@pytest.mark.asyncio
async def test_stop_frame_publisher_calls_stop_and_pops(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    manager = RealBrowserManager()
    await manager._start_frame_publisher(
        browser_state=_marked_browser_state(),
        workflow_run_id="wr_42",
        organization_id="o_1",
    )

    await manager._stop_frame_publisher(workflow_run_id="wr_42")

    assert patched_publisher[0].stop_called == 1
    assert "wr_42.png" not in manager._frame_publishers


@pytest.mark.asyncio
async def test_stop_frame_publisher_is_noop_when_missing(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    manager = RealBrowserManager()
    # Must not raise on stop without start.
    await manager._stop_frame_publisher(workflow_run_id="wr_does_not_exist")
    assert patched_publisher == []


@pytest.mark.asyncio
async def test_stop_frame_publisher_swallows_stop_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingStopPublisher(_RecordingPublisher):
        async def stop(self) -> None:
            raise RuntimeError("kaboom-stop")

    def _factory(**kwargs: object) -> _ExplodingStopPublisher:
        return _ExplodingStopPublisher(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(manager_module, "CDPFramePublisher", _factory)

    manager = RealBrowserManager()
    await manager._start_frame_publisher(
        browser_state=_marked_browser_state(),
        workflow_run_id="wr_42",
        organization_id="o_1",
    )

    # Must not raise.
    await manager._stop_frame_publisher(workflow_run_id="wr_42")
    assert manager._frame_publishers == {}


@pytest.mark.asyncio
async def test_inherited_child_workflow_starts_publisher_for_child_key(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    """Child workflow runs that reuse the parent's browser must still publish to the
    child's own ``{child_workflow_run_id}.png`` stream key. The workflow-run streaming
    endpoint reads that key, so a missing publisher leaves the child livestream blank.
    """
    manager = RealBrowserManager()
    parent_state = MagicMock()
    # Parent was created via a remote-CDP creator; the marker is set to True.
    # Bool literal (not a Mock auto-attribute) so the gate accepts it.
    parent_state.browser_artifacts.needs_cdp_frame_publisher = True
    manager.pages["wfr_parent"] = parent_state

    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wfr_child"
    workflow_run.parent_workflow_run_id = "wfr_parent"
    workflow_run.organization_id = "o_1"
    workflow_run.browser_profile_id = None
    workflow_run.proxy_location = None
    workflow_run.extra_http_headers = None
    workflow_run.cdp_connect_headers = None
    workflow_run.browser_address = None

    result = await manager.get_or_create_for_workflow_run(
        workflow_run=workflow_run,
        url=None,
        browser_session_id=None,
    )

    assert result is parent_state
    assert len(patched_publisher) == 1
    pub = patched_publisher[0]
    assert pub.stream_key == "wfr_child.png"
    assert pub.organization_id == "o_1"
    assert pub.start_called == 1
    assert manager._frame_publishers["wfr_child.png"] is pub


@pytest.mark.asyncio
async def test_close_stops_all_publishers(patched_publisher: list[_RecordingPublisher]) -> None:
    manager = RealBrowserManager()
    await manager._start_frame_publisher(
        browser_state=_marked_browser_state(),
        workflow_run_id="wr_1",
        organization_id="o_1",
    )
    await manager._start_frame_publisher(
        browser_state=_marked_browser_state(),
        workflow_run_id="wr_2",
        organization_id="o_1",
    )

    # Replace browser_state .close() so we don't need real BrowserState plumbing.
    closed_states: list[object] = []

    async def _close_state() -> None:
        closed_states.append(object())

    manager.pages = {
        "wr_1": SimpleNamespace(close=AsyncMock(side_effect=_close_state)),
        "wr_2": SimpleNamespace(close=AsyncMock(side_effect=_close_state)),
    }

    await manager.close()

    assert all(pub.stop_called == 1 for pub in patched_publisher)
    assert manager._frame_publishers == {}
    assert manager.pages == {}


@pytest.mark.asyncio
async def test_cleanup_for_workflow_run_stops_child_publishers(
    patched_publisher: list[_RecordingPublisher],
) -> None:
    """Parent cleanup must stop publishers for every child workflow run id it
    pops. Child workflows skip their own cleanup, so otherwise those publishers
    leak until process shutdown.
    """
    manager = RealBrowserManager()

    # Parent + two inherited child runs all share the same browser state.
    shared_state = SimpleNamespace(
        browser_context=None,
        browser_artifacts=SimpleNamespace(traces_dir=None, needs_cdp_frame_publisher=True),
        close=AsyncMock(),
        add_on_close=lambda _cb: None,
    )
    manager.pages = {
        "wfr_parent": shared_state,
        "wfr_child_a": shared_state,
        "wfr_child_b": shared_state,
    }

    # Start a publisher for each entity, matching the keys the streaming
    # endpoint reads.
    for wr_id in ("wfr_parent", "wfr_child_a", "wfr_child_b"):
        await manager._start_frame_publisher(
            browser_state=shared_state,
            workflow_run_id=wr_id,
            organization_id="o_1",
        )
    assert set(manager._frame_publishers.keys()) == {
        "wfr_parent.png",
        "wfr_child_a.png",
        "wfr_child_b.png",
    }

    await manager.cleanup_for_workflow_run(
        workflow_run_id="wfr_parent",
        task_ids=[],
        close_browser_on_completion=True,
        child_workflow_run_ids=["wfr_child_a", "wfr_child_b"],
    )

    # Every publisher (parent + both children) is stopped and removed from
    # the registry. Without the fix the child publishers would still be in
    # the dict because their stop was never called.
    assert manager._frame_publishers == {}
    child_pubs = [pub for pub in patched_publisher if pub.stream_key.startswith("wfr_child_")]
    assert len(child_pubs) == 2
    assert all(pub.stop_called == 1 for pub in child_pubs)
    # Child entries are popped from the pages map regardless of stream state.
    assert "wfr_child_a" not in manager.pages
    assert "wfr_child_b" not in manager.pages
