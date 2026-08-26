import asyncio
from typing import Any

import pytest

from skyvern.services.browser_recording.v2 import observation
from skyvern.services.browser_recording.v2.ledger import Gesture, GestureKind, GestureLedger, start_ledger, stop_ledger
from skyvern.services.browser_recording.v2.observation import Observer, get_observer, start_observer, stop_observer
from tests.unit.services.browser_recording.v2._fakes import FakeCdpSession


def _gesture(
    *,
    t_received: float,
    kind: GestureKind = "mouse_pressed",
    key: str | None = None,
    page_key: str = "page-1",
) -> Gesture:
    return Gesture(
        seq=0,
        t_received=t_received,
        kind=kind,
        page_key=page_key,
        url="https://example.test/start",
        key=key,
    )


@pytest.mark.asyncio
async def test_page_enables_are_sent_together_after_listener_registration() -> None:
    started = {"Page.enable": asyncio.Event(), "Network.enable": asyncio.Event()}

    class _ConcurrentEnableSession(FakeCdpSession):
        async def send(self, method: str, params: dict[str, Any]) -> None:
            await super().send(method, params)
            started[method].set()
            other = "Network.enable" if method == "Page.enable" else "Page.enable"
            await started[other].wait()

    ledger = GestureLedger("pbs-concurrent-enables")
    session = _ConcurrentEnableSession()

    await asyncio.wait_for(Observer(ledger).attach_page_session("page-1", session), timeout=0.1)

    assert all("Page.frameNavigated" in listeners for listeners in session.listeners_at_send)


@pytest.mark.asyncio
async def test_navigation_attaches_to_a_recent_click_and_is_otherwise_standalone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1.0
    monkeypatch.setattr(observation, "monotonic", lambda: now)
    ledger = GestureLedger("pbs-observation")
    click = ledger.append(_gesture(t_received=now))
    ledger.append(_gesture(t_received=1.2, page_key="page-2"))
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_page_session("page-1", session)

    now = 1.5
    session.fire(
        "Page.frameStartedNavigating",
        {"frameId": "frame-1", "url": "https://example.test/redirecting"},
    )
    now = 2.0
    session.fire("Page.frameNavigated", {"frame": {"id": "frame-1", "url": "https://example.test/next"}})
    now = 5.0
    session.fire("Page.frameNavigated", {"frame": {"id": "frame-1", "url": "https://example.test/later"}})
    session.fire(
        "Page.frameNavigated",
        {"frame": {"id": "frame-2", "parentId": "frame-1", "url": "https://example.test/embedded"}},
    )

    assert [(effect.seq, effect.url, effect.caused_by_seq, effect.is_main_frame) for effect in ledger.effects()] == [
        (3, "https://example.test/next", click.seq, True),
        (4, "https://example.test/later", None, True),
        (5, "https://example.test/embedded", None, False),
    ]


@pytest.mark.asyncio
async def test_stale_pending_navigation_does_not_swallow_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1.0
    monkeypatch.setattr(observation, "monotonic", lambda: now)
    ledger = GestureLedger("pbs-stale-navigation")
    click = ledger.append(_gesture(t_received=now))
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_page_session("page-1", session)

    session.fire("Page.frameStartedNavigating", {"frameId": "frame-1", "url": "https://example.test/start"})
    now = 5.0
    session.fire("Page.frameNavigated", {"frame": {"id": "frame-1", "url": "https://example.test/end"}})

    assert [(effect.url, effect.caused_by_seq) for effect in ledger.effects()] == [
        ("https://example.test/start", click.seq),
        ("https://example.test/end", None),
    ]


@pytest.mark.asyncio
async def test_navigation_that_completes_while_paused_does_not_backfill_the_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1.0
    monkeypatch.setattr(observation, "monotonic", lambda: now)
    ledger = GestureLedger("pbs-paused-navigation")
    click = ledger.append(_gesture(t_received=now))
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_page_session("page-1", session)

    session.fire("Page.frameStartedNavigating", {"frameId": "frame-1", "url": "https://example.test/start"})
    ledger.paused = True
    now = 1.5
    session.fire("Page.frameNavigated", {"frame": {"id": "frame-1", "url": "https://example.test/kept-out"}})

    assert [(effect.url, effect.caused_by_seq) for effect in ledger.effects()] == [
        ("https://example.test/start", click.seq),
    ]


@pytest.mark.asyncio
async def test_network_activity_settles_once_for_the_preceding_gesture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1.0
    monkeypatch.setattr(observation, "monotonic", lambda: now)
    monkeypatch.setattr(observation, "SETTLE_QUIET_MS", 1)
    ledger = GestureLedger("pbs-settle")
    click = ledger.append(_gesture(t_received=now))
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_page_session("page-1", session)

    now = 1.1
    session.fire("Network.requestWillBeSent", {"requestId": "1", "frameId": "frame-1"})
    now = 1.3
    session.fire("Network.loadingFinished", {"requestId": "1"})
    await asyncio.sleep(0.01)

    assert [(effect.kind, effect.caused_by_seq, effect.busy_ms) for effect in ledger.effects()] == [
        ("network_settle", click.seq, 300)
    ]

    no_gesture_ledger = GestureLedger("pbs-no-gesture")
    no_gesture_session = FakeCdpSession()
    no_gesture_observer = Observer(no_gesture_ledger)
    await no_gesture_observer.attach_page_session("page-1", no_gesture_session)
    now = 2.0
    no_gesture_session.fire("Network.requestWillBeSent", {"requestId": "2", "frameId": "frame-1"})
    await asyncio.sleep(0.01)

    assert no_gesture_ledger.effects() == []


@pytest.mark.asyncio
async def test_network_settle_survives_moves_and_anchors_to_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1.0
    monkeypatch.setattr(observation, "monotonic", lambda: now)
    monkeypatch.setattr(observation, "SETTLE_QUIET_MS", 1)
    ledger = GestureLedger("pbs-moving-settle")
    click = ledger.append(_gesture(t_received=now))
    for kind in ("mouse_released", "wheel", "mouse_moved"):
        now += 0.1
        ledger.append(_gesture(t_received=now, kind=kind))
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_page_session("page-1", session)

    now = 1.4
    session.fire("Network.requestWillBeSent", {"requestId": "1"})
    now = 1.5
    ledger.append(_gesture(t_received=now, kind="mouse_moved"))
    await asyncio.sleep(0.01)

    assert [(effect.caused_by_seq, effect.busy_ms) for effect in ledger.effects()] == [(click.seq, 400)]


@pytest.mark.asyncio
async def test_network_settle_is_capped_after_continuous_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1.0
    monkeypatch.setattr(observation, "monotonic", lambda: now)
    ledger = GestureLedger("pbs-capped-settle")
    click = ledger.append(_gesture(t_received=now))
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_page_session("page-1", session)

    for now in (2.0, 10.0, 16.1):
        session.fire("Network.requestWillBeSent", {"requestId": str(now)})

    assert [(effect.caused_by_seq, effect.busy_ms, effect.detail) for effect in ledger.effects()] == [
        (click.seq, 15_000, {"capped": True})
    ]


@pytest.mark.asyncio
async def test_dialog_and_file_chooser_are_observed_without_interception() -> None:
    ledger = GestureLedger("pbs-page-effects")
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_page_session("page-1", session)

    session.fire("Page.javascriptDialogOpening", {"type": "alert", "message": "page text"})
    session.fire("Page.fileChooserOpened", {"frameId": "frame-1", "mode": "selectSingle", "backendNodeId": 9})

    assert [(effect.kind, effect.detail) for effect in ledger.effects()] == [
        ("dialog", {"type": "alert"}),
        ("file_chooser", {"mode": "selectSingle", "backendNodeId": 9}),
    ]
    assert "Page.handleJavaScriptDialog" not in [method for method, _ in session.sent]
    assert "Page.setInterceptFileChooserDialog" not in [method for method, _ in session.sent]
    assert all("Page.javascriptDialogOpening" in listeners for listeners in session.listeners_at_send)


@pytest.mark.asyncio
async def test_detach_removes_listeners() -> None:
    ledger = GestureLedger("pbs-detach")
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_page_session("page-1", session)

    observer.detach()
    session.fire("Page.javascriptDialogOpening", {"type": "confirm"})
    session.fire("Page.frameNavigated", {"frame": {"id": "frame-1", "url": "https://example.test/next"}})

    assert ledger.effects() == []


@pytest.mark.asyncio
async def test_browser_targets_are_filtered_and_deduplicated() -> None:
    ledger = GestureLedger("pbs-targets")
    session = FakeCdpSession()
    observer = Observer(ledger)
    await observer.attach_browser_session(session)

    session.fire(
        "Target.targetCreated",
        {"targetInfo": {"targetId": "worker-1", "type": "service_worker", "url": "https://example.test/worker"}},
    )
    page = {"targetInfo": {"targetId": "page-1", "type": "page", "url": "https://example.test/one"}}
    session.fire("Target.targetCreated", page)
    session.fire("Target.targetCreated", page)
    session.fire("Target.targetInfoChanged", page)
    session.fire(
        "Target.targetInfoChanged",
        {"targetInfo": {"targetId": "page-1", "type": "page", "url": "https://example.test/two"}},
    )

    assert [(effect.target_id, effect.url) for effect in ledger.effects()] == [
        ("page-1", "https://example.test/one"),
        ("page-1", "https://example.test/two"),
    ]
    assert set(session.listeners) == {"Target.targetCreated", "Target.targetInfoChanged"}
    assert session.listeners_at_send == [{"Target.targetCreated", "Target.targetInfoChanged"}]


@pytest.mark.asyncio
async def test_stop_ledger_detaches_observer_via_hook() -> None:
    browser_session_id = "pbs-stop-observer"
    ledger = start_ledger(browser_session_id)
    observer = start_observer(browser_session_id, ledger)
    session = FakeCdpSession()
    await observer.attach_page_session("page-1", session)

    try:
        stop_ledger(browser_session_id)

        assert all(not callbacks for callbacks in session.listeners.values())
        assert get_observer(browser_session_id) is None
        replacement_ledger = start_ledger(browser_session_id)
        replacement_observer = start_observer(browser_session_id, replacement_ledger)
        assert replacement_observer is not observer
        assert replacement_observer.ledger is replacement_ledger
    finally:
        stop_ledger(browser_session_id)
        stop_observer(browser_session_id)
