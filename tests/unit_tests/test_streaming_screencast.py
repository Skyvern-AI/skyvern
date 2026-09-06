from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from fastapi import WebSocketDisconnect

from skyvern.forge.sdk.encrypt.base import TokenDecryptionError
from tests.unit.scoped_asyncio import ScopedAsyncio
from tests.unit_tests._stub_streaming import import_with_stubs

screencast = import_with_stubs(
    "skyvern.forge.sdk.routes.streaming.screencast",
    extra_stubs=["skyvern.forge.sdk.routes.streaming.screenshot"],
)


def _make_app(browser_manager=None, persistent_sessions_manager=None):
    """Build a fake app namespace to replace screencast.app (an AppHolder proxy)."""
    return SimpleNamespace(
        BROWSER_MANAGER=browser_manager or SimpleNamespace(),
        PERSISTENT_SESSIONS_MANAGER=persistent_sessions_manager or SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_resolve_browser_state_for_workflow_run(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_state = object()
    fake_app = _make_app(
        browser_manager=SimpleNamespace(get_for_workflow_run=Mock(return_value=expected_state), get_for_task=Mock()),
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock()),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_browser_state("wr_123", "workflow_run")

    assert result is expected_state
    fake_app.BROWSER_MANAGER.get_for_workflow_run.assert_called_once_with("wr_123")
    fake_app.BROWSER_MANAGER.get_for_task.assert_not_called()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_browser_state_for_task(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_state = object()
    fake_app = _make_app(
        browser_manager=SimpleNamespace(get_for_workflow_run=Mock(), get_for_task=Mock(return_value=expected_state)),
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock()),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_browser_state("task_123", "task", workflow_run_id="wr_123")

    assert result is expected_state
    fake_app.BROWSER_MANAGER.get_for_task.assert_called_once_with("task_123", "wr_123")
    fake_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_browser_state_for_browser_session(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_state = object()
    fake_app = _make_app(
        browser_manager=SimpleNamespace(get_for_workflow_run=Mock(), get_for_task=Mock()),
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock(return_value=expected_state)),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_browser_state("bs_123", "browser_session", organization_id="o_123")

    assert result is expected_state
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_awaited_once_with("bs_123", "o_123")
    fake_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()
    fake_app.BROWSER_MANAGER.get_for_task.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_browser_state_unknown_entity_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = _make_app(
        browser_manager=SimpleNamespace(get_for_workflow_run=Mock(), get_for_task=Mock()),
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock()),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_browser_state("id_123", "unknown")

    assert result is None
    fake_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()
    fake_app.BROWSER_MANAGER.get_for_task.assert_not_called()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_browser_state_returns_when_working_page_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=object()))
    resolve_mock = AsyncMock(return_value=browser_state)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(screencast, "_resolve_browser_state", resolve_mock)
    monkeypatch.setattr(screencast, "asyncio", SimpleNamespace(sleep=sleep_mock))

    result = await screencast.wait_for_browser_state("wr_123", "workflow_run", timeout=1, poll_interval=0.1)

    assert result is browser_state
    resolve_mock.assert_awaited_once_with("wr_123", "workflow_run", None, organization_id=None)
    browser_state.get_working_page.assert_awaited_once()
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_browser_state_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=None))
    resolve_mock = AsyncMock(return_value=browser_state)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(screencast, "_resolve_browser_state", resolve_mock)
    monkeypatch.setattr(screencast, "asyncio", ScopedAsyncio(sleep=sleep_mock))

    result = await screencast.wait_for_browser_state(
        "wr_123",
        "workflow_run",
        timeout=0.3,
        poll_interval=0.1,
    )

    assert result is None
    assert resolve_mock.await_count == 3
    assert browser_state.get_working_page.await_count == 3


@pytest.mark.asyncio
async def test_wait_for_browser_state_gives_up_when_the_org_token_cannot_be_decrypted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The candidate key set is fixed for the process, so every remaining poll would pay a browser
    attach to fail identically. SKY-15074 burned 480 attaches per websocket that way."""
    resolve_mock = AsyncMock(side_effect=TokenDecryptionError("Failed to decrypt token"))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(screencast, "_resolve_browser_state", resolve_mock)
    monkeypatch.setattr(screencast, "asyncio", ScopedAsyncio(sleep=sleep_mock))

    result = await screencast.wait_for_browser_state(
        "pbs_1",
        "browser_session",
        timeout=0.3,
        poll_interval=0.1,
        organization_id="o_1",
    )

    assert result is None
    assert resolve_mock.await_count == 1
    sleep_mock.assert_not_awaited()


@pytest.mark.parametrize(
    ("entity_id", "entity_type", "kwargs"),
    [
        ("wr_1", "workflow_run", {}),
        ("tsk_1", "task", {"workflow_run_id": "wr_1"}),
    ],
)
@pytest.mark.asyncio
async def test_a_run_without_in_process_state_never_adopts_the_session(
    monkeypatch: pytest.MonkeyPatch, entity_id: str, entity_type: str, kwargs: dict
) -> None:
    """A run's frames come from the worker that drives it, so the API process resolves a run only
    against its own manager. Reaching into the session instead would adopt a browser the worker is
    driving — and would miss anyway, since a task inside a workflow leases under the run's id."""
    fake_app = _make_app(
        browser_manager=SimpleNamespace(
            get_for_workflow_run=Mock(return_value=None), get_for_task=Mock(return_value=None)
        ),
        persistent_sessions_manager=SimpleNamespace(
            get_session_by_runnable_id=AsyncMock(),
            get_browser_state=AsyncMock(),
            get_observer_browser_state=AsyncMock(),
        ),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    assert await screencast._resolve_browser_state(entity_id, entity_type, organization_id="o_1", **kwargs) is None

    fake_app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id.assert_not_awaited()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_not_awaited()
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_out_a_session_holds_one_connection_and_gives_it_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each resolve mints its own connection, so re-resolving per poll would leave one adopted
    browser per tick behind."""
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=None))
    release = AsyncMock()
    fake_app = _make_app(
        persistent_sessions_manager=SimpleNamespace(
            get_observer_browser_state=AsyncMock(return_value=browser_state),
            release_observer_browser_state=release,
        ),
    )
    monkeypatch.setattr(screencast, "app", fake_app)
    monkeypatch.setattr(screencast, "asyncio", ScopedAsyncio(sleep=AsyncMock()))

    result = await screencast.wait_for_browser_state(
        "bs_123", "browser_session", organization_id="o_1", timeout=0.3, poll_interval=0.1
    )

    assert result is None
    assert fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.await_count == 1
    assert browser_state.get_working_page.await_count == 3
    release.assert_awaited_once_with("bs_123", browser_state)


@pytest.mark.asyncio
async def test_following_the_active_page_does_not_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    page = object()
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=page))
    fake_app = _make_app(
        persistent_sessions_manager=SimpleNamespace(get_observer_browser_state=AsyncMock()),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    result = await screencast._resolve_working_page(browser_state, "bs_123", "browser_session", organization_id="o_1")

    assert result is page
    fake_app.PERSISTENT_SESSIONS_MANAGER.get_observer_browser_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_run_state_is_not_the_viewers_to_release(monkeypatch: pytest.MonkeyPatch) -> None:
    release = AsyncMock()
    fake_app = _make_app(
        persistent_sessions_manager=SimpleNamespace(release_observer_browser_state=release),
    )
    monkeypatch.setattr(screencast, "app", fake_app)

    await screencast.release_browser_state(SimpleNamespace(), "workflow_run", "wr_1")

    release.assert_not_awaited()


class _FakeCdpSession:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.sent: list[tuple[str, dict]] = []
        self.detached = False
        self.fail_methods: set[str] = set()
        self.fail_after_calls: dict[str, int] = {}

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        if method in self.fail_methods and self.methods_sent().count(method) > self.fail_after_calls.get(method, 0):
            raise RuntimeError(f"{method} refused")
        if method == "Page.captureScreenshot":
            return {"data": "primed"}
        return {}

    async def detach(self) -> None:
        self.detached = True

    def methods_sent(self) -> list[str]:
        return [method for method, _ in self.sent]


class _FakePage:
    def __init__(self, session: _FakeCdpSession) -> None:
        self.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
        self.url = "https://example.test/"
        self.viewport_size = {"width": 800, "height": 600}


def _connected_websocket(send_json: object) -> SimpleNamespace:
    """A real WebSocket is readable, and the screencast loop reads it to notice a client disconnect."""

    async def _receive() -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return SimpleNamespace(send_json=send_json, receive=_receive)


async def _wait_for(predicate: Callable[[], bool]) -> bool:
    for _ in range(500):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_forwarded_frame_is_acked_and_records_forward_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeCdpSession()
    page = _FakePage(session)
    monkeypatch.setattr(screencast, "_resolve_working_page", AsyncMock(return_value=page))
    forward_hist, queue_hist, send_hist = Mock(), Mock(), Mock()
    monkeypatch.setattr(screencast, "_frame_forward_seconds", forward_hist)
    monkeypatch.setattr(screencast, "_frame_queue_seconds", queue_hist)
    monkeypatch.setattr(screencast, "_frame_send_seconds", send_hist)

    sent: list[dict] = []

    async def _send_json(payload: dict) -> None:
        sent.append(payload)
        if len(sent) == 2:
            raise RuntimeError("client gone")

    websocket = _connected_websocket(_send_json)

    async def _never_finalized() -> bool:
        return False

    loop_task = asyncio.create_task(
        screencast.start_screencast_loop(websocket, object(), "pbs_1", "browser_session", _never_finalized)
    )
    for _ in range(100):
        if "Page.screencastFrame" in session.handlers:
            break
        await asyncio.sleep(0.01)
    session.handlers["Page.screencastFrame"](
        {"data": "AAAA", "sessionId": 7, "metadata": {"deviceWidth": 640, "deviceHeight": 480}}
    )
    await asyncio.wait_for(loop_task, timeout=5)

    assert [m["screenshot"] for m in sent] == ["primed", "AAAA"]
    assert sent[1]["viewport_width"] == 640 and sent[1]["viewport_height"] == 480
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in session.sent
    assert queue_hist.record.call_count == 2
    assert forward_hist.record.call_count == 1 and send_hist.record.call_count == 1
    value, attributes = forward_hist.record.call_args.args
    assert value >= 0 and attributes == {"entity_type": "browser_session"}


@pytest.mark.asyncio
async def test_client_disconnect_is_raised_so_the_caller_does_not_write_a_final_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeCdpSession()
    monkeypatch.setattr(screencast, "_resolve_working_page", AsyncMock(return_value=_FakePage(session)))
    websocket = SimpleNamespace(
        send_json=AsyncMock(),
        receive=AsyncMock(return_value={"type": "websocket.disconnect", "code": 1006}),
    )

    async def _never_finalized() -> bool:
        return False

    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(
            screencast.start_screencast_loop(websocket, object(), "pbs_1", "browser_session", _never_finalized),
            timeout=5,
        )
    assert session.detached


@pytest.mark.asyncio
async def test_evicted_frame_records_queue_dwell(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeCdpSession()
    page = _FakePage(session)
    monkeypatch.setattr(screencast, "_resolve_working_page", AsyncMock(return_value=page))
    queue_hist, evicted = Mock(), Mock()
    monkeypatch.setattr(screencast, "_frame_queue_seconds", queue_hist)
    monkeypatch.setattr(screencast, "_frames_evicted", evicted)

    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    sent: list[dict] = []

    async def _send_json(payload: dict) -> None:
        sent.append(payload)
        if payload["screenshot"] == "primed":
            first_send_started.set()
            await release_first_send.wait()
        elif payload["screenshot"] == "frame-3":
            raise RuntimeError("client gone")

    websocket = _connected_websocket(_send_json)

    async def _never_finalized() -> bool:
        return False

    loop_task = asyncio.create_task(
        screencast.start_screencast_loop(websocket, object(), "pbs_1", "browser_session", _never_finalized)
    )
    for _ in range(100):
        if "Page.screencastFrame" in session.handlers:
            break
        await asyncio.sleep(0.01)
    await asyncio.wait_for(first_send_started.wait(), timeout=5)

    try:
        for data in ("frame-1", "frame-2", "frame-3"):
            session.handlers["Page.screencastFrame"]({"data": data, "sessionId": 7, "metadata": {}})
            await asyncio.sleep(0)

        for _ in range(100):
            if evicted.add.called:
                break
            await asyncio.sleep(0.01)

        assert evicted.add.call_count == 1
        # The forwarding loop has dequeued only the primed frame, so the second sample
        # can only come from the oldest queued frame that was evicted.
        assert queue_hist.record.call_count == 2
        value, attributes = queue_hist.record.call_args.args
        assert value >= 0 and attributes == {"entity_type": "browser_session"}
    finally:
        release_first_send.set()
        await asyncio.wait_for(loop_task, timeout=5)

    assert [payload["screenshot"] for payload in sent] == ["primed", "frame-2", "frame-3"]


@pytest.mark.asyncio
async def test_a_page_that_cannot_be_attached_does_not_kill_the_running_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_session = _FakeCdpSession()
    current_page = _FakePage(current_session)
    next_session = _FakeCdpSession()
    next_page = _FakePage(next_session)
    attach_fails = True

    async def _new_cdp_session(_page: object) -> _FakeCdpSession:
        if attach_fails:
            raise RuntimeError("Target closed")
        return next_session

    next_page.context.new_cdp_session = AsyncMock(side_effect=_new_cdp_session)
    monkeypatch.setattr(screencast, "ACTIVE_PAGE_POLL_INTERVAL", 0.01)
    # Cap the backoff too: a slow runner could otherwise spend long enough failing to attach
    # that the poll interval grows past this test's own wait budget.
    monkeypatch.setattr(screencast, "ACTIVE_PAGE_MAX_POLL_INTERVAL", 0.05)

    pages = iter([current_page])

    async def _resolve_working_page(*_args: object, **_kwargs: object) -> object:
        return next(pages, next_page)

    monkeypatch.setattr(screencast, "_resolve_working_page", _resolve_working_page)

    sent: list[dict] = []

    async def _send_json(payload: dict) -> None:
        sent.append(payload)
        if payload["screenshot"] == "frame-from-next-page":
            raise RuntimeError("client gone")

    websocket = _connected_websocket(_send_json)

    async def _never_finalized() -> bool:
        return False

    loop_task = asyncio.create_task(
        screencast.start_screencast_loop(websocket, object(), "pbs_1", "browser_session", _never_finalized)
    )
    assert await _wait_for(lambda: bool(next_page.context.new_cdp_session.await_count))

    assert current_session.detached is False
    assert "Page.stopScreencast" not in current_session.methods_sent()
    current_session.handlers["Page.screencastFrame"](
        {"data": "frame-from-current-page", "sessionId": 7, "metadata": {}}
    )
    assert await _wait_for(lambda: any(payload["screenshot"] == "frame-from-current-page" for payload in sent))

    attach_fails = False
    assert await _wait_for(lambda: "Page.startScreencast" in next_session.methods_sent())
    assert await _wait_for(lambda: current_session.detached)
    next_session.handlers["Page.screencastFrame"]({"data": "frame-from-next-page", "sessionId": 9, "metadata": {}})
    await asyncio.wait_for(loop_task, timeout=5)

    assert [payload["screenshot"] for payload in sent] == [
        "primed",
        "frame-from-current-page",
        "primed",
        "frame-from-next-page",
    ]


class _FramesDuringStartSession(_FakeCdpSession):
    """Emits a frame before Page.startScreencast returns, i.e. before the loop can adopt it."""

    async def send(self, method: str, params: dict | None = None) -> dict:
        result = await super().send(method, params)
        if method == "Page.startScreencast":
            self.handlers["Page.screencastFrame"]({"data": "mid-swap", "sessionId": 11, "metadata": {}})
            await asyncio.sleep(0)
        return result


@pytest.mark.asyncio
async def test_a_frame_that_lands_mid_swap_is_still_acked(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FramesDuringStartSession()
    page = _FakePage(session)
    monkeypatch.setattr(screencast, "_resolve_working_page", AsyncMock(return_value=page))

    sent: list[dict] = []

    async def _send_json(payload: dict) -> None:
        sent.append(payload)
        raise RuntimeError("client gone")

    async def _never_finalized() -> bool:
        return False

    await asyncio.wait_for(
        screencast.start_screencast_loop(
            _connected_websocket(_send_json), object(), "pbs_1", "browser_session", _never_finalized
        ),
        timeout=5,
    )

    # Chrome stops producing frames for a session whose frames go unacked, so the ack has to happen
    # even though the frame arrived too early to be forwarded.
    assert ("Page.screencastFrameAck", {"sessionId": 11}) in session.sent
    assert [payload["screenshot"] for payload in sent] == ["primed"]


_REAL_SLEEP = asyncio.sleep


class _VirtualClock:
    """Instant sleeps over a virtual monotonic clock, recorded per calling task.

    Grouping by task keeps the completion poller's one sleep out of the page monitor's sequence.
    """

    def __init__(self, stop_after: int) -> None:
        self.by_task: dict[int, list[float]] = {}
        self.now = 0.0
        self._stop_after = stop_after
        self.parked = asyncio.Event()

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        recorded = self.by_task.setdefault(id(asyncio.current_task()), [])
        recorded.append(delay)
        self.now += delay
        if len(recorded) >= self._stop_after:
            self.parked.set()
            await asyncio.Event().wait()
        await _REAL_SLEEP(0)

    @property
    def poll_delays(self) -> list[float]:
        return max(self.by_task.values(), key=len, default=[])


def _scripted_pages(*script: object):
    """Resolve pages in order, then hold the final entry forever."""
    remaining = list(script)

    async def _resolve(*args: object, **kwargs: object) -> object:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _resolve


async def _drive_page_monitor(
    monkeypatch: pytest.MonkeyPatch,
    clock: _VirtualClock,
    *script: object,
    entity_id: str = "pbs_1",
    entity_type: str = "browser_session",
    **loop_kwargs: object,
) -> asyncio.Task:
    monkeypatch.setattr(screencast, "_resolve_working_page", _scripted_pages(*script))
    monkeypatch.setattr(screencast, "asyncio", ScopedAsyncio(sleep=clock.sleep))
    monkeypatch.setattr(screencast, "time", SimpleNamespace(monotonic=clock.monotonic))

    async def _never_finalized() -> bool:
        await asyncio.Event().wait()
        return False

    task = asyncio.create_task(
        screencast.start_screencast_loop(
            _connected_websocket(AsyncMock()),
            object(),
            entity_id,
            entity_type,
            _never_finalized,
            **loop_kwargs,
        )
    )
    await asyncio.wait_for(clock.parked.wait(), timeout=5)
    return task


async def _stop(task: asyncio.Task) -> None:
    # Cancelling start_screencast_loop mid-wait leaves its child tasks running, so drain them too.
    task.cancel()
    for pending in asyncio.all_tasks() - {asyncio.current_task()}:
        pending.cancel()
        with contextlib.suppress(BaseException):
            await pending


def _messages(calls: list, message: str) -> list:
    return [call for call in calls if call.args and call.args[0] == message]


@pytest.mark.asyncio
async def test_page_poll_holds_the_fast_cadence_then_backs_off_and_clamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary page-less gaps clear in a few seconds, so the fast cadence is held past them; only a
    wedged viewer reaches the backoff, and it must stop doubling at the ceiling rather than growing."""
    clock = _VirtualClock(stop_after=16)
    task = await _drive_page_monitor(monkeypatch, clock, _FakePage(_FakeCdpSession()), None)

    try:
        assert clock.poll_delays == [0.5] * 9 + [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_degraded_entry_is_reported_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the bound: one wedged viewer used to log on every poll."""
    log = Mock()
    monkeypatch.setattr(screencast, "LOG", log)
    clock = _VirtualClock(stop_after=60)
    task = await _drive_page_monitor(
        monkeypatch,
        clock,
        _FakePage(_FakeCdpSession()),
        None,
        entity_id="pbs_1",
        entity_type="browser_session",
        organization_id="o_1",
    )

    try:
        entries = _messages(log.warning.call_args_list, "Live view cannot follow the active page; backing off")
        assert len(clock.poll_delays) == 60
        assert len(entries) == 1
        assert entries[0].kwargs["browser_session_id"] == "pbs_1"
        assert entries[0].kwargs["entity_id"] == "pbs_1"
        assert entries[0].kwargs["entity_type"] == "browser_session"
        assert entries[0].kwargs["organization_id"] == "o_1"
        assert entries[0].kwargs["degraded_for_seconds"] == 4.0
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_a_page_appearing_mid_backoff_recovers_and_reports_the_degraded_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Mock()
    monkeypatch.setattr(screencast, "LOG", log)
    clock = _VirtualClock(stop_after=15)
    recovered = _FakePage(_FakeCdpSession())
    task = await _drive_page_monitor(
        monkeypatch,
        clock,
        _FakePage(_FakeCdpSession()),
        *([None] * 12),
        recovered,
    )

    try:
        recoveries = _messages(log.info.call_args_list, "Live view is following the active page again")
        assert len(recoveries) == 1
        # Empty from t=0.5 until the page came back at t=19.5, across the 0.5->8.0 backoff.
        assert recoveries[0].kwargs["degraded_for_seconds"] == 19.0
        assert recoveries[0].kwargs["browser_session_id"] == "pbs_1"
        # The screencast re-attached to the page that came back, and the fast cadence resumed.
        assert ("Page.startScreencast", ANY) in recovered.context.new_cdp_session.return_value.sent
        assert clock.poll_delays[13:] == [0.5, 0.5]
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_the_page_monitor_survives_a_long_degraded_stretch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backing off is not giving up: the viewer stays connected so a page that returns is followed."""
    clock = _VirtualClock(stop_after=200)
    task = await _drive_page_monitor(monkeypatch, clock, _FakePage(_FakeCdpSession()), None)

    try:
        assert not task.done()
        assert len(clock.poll_delays) == 200
        assert set(clock.poll_delays[20:]) == {30.0}
    finally:
        await _stop(task)


class _UnattachablePage:
    """Resolves fine, but every attempt to open a CDP session on it fails."""

    def __init__(self) -> None:
        self.context = SimpleNamespace(new_cdp_session=AsyncMock(side_effect=RuntimeError("cdp session refused")))
        self.url = "https://example.test/"
        self.viewport_size = {"width": 800, "height": 600}


@pytest.mark.asyncio
async def test_a_page_that_never_attaches_backs_off_like_a_missing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page the viewer cannot attach to is as dead as no page: without this the loop would sit at
    2 Hz forever on a failing attach, which is the pathology the backoff exists to stop."""
    log = Mock()
    monkeypatch.setattr(screencast, "LOG", log)
    clock = _VirtualClock(stop_after=12)
    task = await _drive_page_monitor(
        monkeypatch,
        clock,
        _FakePage(_FakeCdpSession()),
        _UnattachablePage(),
    )

    try:
        assert clock.poll_delays == [0.5] * 9 + [1.0, 2.0, 4.0]
        entries = _messages(log.warning.call_args_list, "Live view cannot follow the active page; backing off")
        assert len(entries) == 1
    finally:
        await _stop(task)
