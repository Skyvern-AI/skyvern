from collections.abc import Iterator

import pytest

from skyvern.services.browser_recording.v2 import session as session_module
from skyvern.services.browser_recording.v2.ledger import Effect, Gesture, GestureKind, get_ledger, stop_ledger
from skyvern.services.browser_recording.v2.session import (
    RecordingSessionV2,
    RecordingUpdateV2,
    discard_session_v2,
    start_session_v2,
    stop_session_v2,
)
from tests.unit.services.browser_recording.v2._fakes import FakeCdpSession

PBS_ID = "pbs_session_v2"
URL = "https://example.test/form"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    discard_session_v2(PBS_ID)
    stop_ledger(PBS_ID)
    yield
    discard_session_v2(PBS_ID)
    stop_ledger(PBS_ID)


def _start(updates: list[RecordingUpdateV2]) -> RecordingSessionV2:
    return start_session_v2(
        browser_session_id=PBS_ID,
        organization_id="org_123",
        workflow_permanent_id="wpid_123",
        on_update=updates.append,
    )


def _gesture(seq: int, kind: GestureKind, **kwargs: object) -> Gesture:
    return Gesture(seq=seq, t_received=float(seq), kind=kind, page_key="page-1", url=URL, **kwargs)


@pytest.mark.asyncio
async def test_scripted_gestures_and_effects_become_deterministic_steps() -> None:
    updates: list[RecordingUpdateV2] = []
    session = _start(updates)
    ledger = get_ledger(PBS_ID)
    assert ledger is not None

    click = ledger.append(
        _gesture(0, "mouse_pressed", x=10, y=20, button="left", click_count=1, accessible_name="Search")
    )
    ledger.append(_gesture(0, "mouse_released", x=10, y=20, button="left", click_count=1))
    ledger.append(_gesture(0, "key", key="h", text="h", key_event_type="keyDown", accessible_name="Search"))
    ledger.append(_gesture(0, "key", key="i", text="i", key_event_type="keyDown"))
    ledger.append_effect(
        Effect(seq=0, t_received=9.0, kind="network_settle", page_key="page-1", caused_by_seq=click.seq, busy_ms=420)
    )
    ledger.append_effect(
        Effect(
            seq=0,
            t_received=10.0,
            kind="navigation",
            page_key="page-1",
            url="https://example.test/results?q=hi",
            is_main_frame=True,
        )
    )
    ledger.append_effect(
        Effect(seq=0, t_received=11.0, kind="navigation", page_key="page-1", url="https://ads.test/frame")
    )

    session.interpret()
    steps = session.steps

    assert [(step.kind, step.title) for step in steps] == [
        ("click", "Click Search"),
        ("type_text", "Type into Search"),
        ("goto_url", "Go to example.test/results"),
    ]
    assert steps[0].step_id == f"{PBS_ID}:1"
    assert steps[0].settle_ms == 420
    assert steps[1].typed_length == 2
    assert steps[1].gesture_seqs == [3, 4]
    assert updates[-1].session_revision == len(updates)


@pytest.mark.asyncio
async def test_pause_drops_gestures_and_resume_restores_capture() -> None:
    from skyvern.services.browser_recording.v2.tap import tap_pipelined

    updates: list[RecordingUpdateV2] = []
    session = _start(updates)
    page = object()

    session.pause()
    tap_pipelined(PBS_ID, "keyEvent", {"type": "keyDown", "key": "a", "text": "a"}, 1.0, page, None)
    paused_effect = session.ledger.append_effect(
        Effect(seq=0, t_received=1.0, kind="navigation", page_key="page-1", url=URL, is_main_frame=True)
    )
    session.interpret()
    assert paused_effect is None
    assert session.ledger.effects() == []
    assert session.steps == []

    session.resume()
    tap_pipelined(PBS_ID, "keyEvent", {"type": "keyDown", "key": "b", "text": "b"}, 2.0, page, None)
    session.interpret()
    assert [step.typed_length for step in session.steps] == [1]


@pytest.mark.asyncio
async def test_seal_returns_final_steps_and_marks_finalized() -> None:
    updates: list[RecordingUpdateV2] = []
    session = _start(updates)
    ledger = get_ledger(PBS_ID)
    assert ledger is not None
    ledger.append(_gesture(0, "mouse_pressed", x=1, y=2, button="left", click_count=1, tag="BUTTON"))

    steps = await stop_session_v2(PBS_ID)

    assert [step.title for step in steps] == ["Click button"]
    assert session.sealed is True
    assert updates[-1].finalized is True
    assert updates[-1].pending is False
    assert updates[-1].is_snapshot is True
    assert updates[-1].steps == steps


@pytest.mark.asyncio
async def test_stopping_leaves_no_capture_behind() -> None:
    updates: list[RecordingUpdateV2] = []
    session = _start(updates)
    cdp_session = FakeCdpSession()
    await session.attach_page("page-1", cdp_session)
    ledger = get_ledger(PBS_ID)
    assert ledger is not None
    ledger.append(_gesture(0, "mouse_pressed", x=1, y=2, button="left", click_count=1, tag="BUTTON"))

    await stop_session_v2(PBS_ID)

    assert ledger.append(_gesture(0, "key", key="a", text="a")) is None
    assert ledger.append_effect(Effect(seq=0, t_received=1.0, kind="navigation", page_key="page-1")) is None
    assert [step.title for step in session.steps] == ["Click button"]
    assert get_ledger(PBS_ID) is None
    assert all(not callbacks for callbacks in cdp_session.listeners.values())


@pytest.mark.asyncio
async def test_abandoned_unsealed_sessions_are_capped_so_their_ledgers_cannot_leak() -> None:
    """A socket that drops without sealing keeps its session on purpose so a reconnect can
    resume it, so nothing else ever reaches stop_ledger for a client that never comes back."""
    ids = [f"pbs_abandoned_{index}" for index in range(session_module.MAX_LIVE_SESSIONS + 1)]
    try:
        for browser_session_id in ids:
            start_session_v2(
                browser_session_id=browser_session_id,
                organization_id="org_123",
                workflow_permanent_id=None,
                on_update=lambda _update: None,
            )

        assert len(session_module.sessions_v2) <= session_module.MAX_LIVE_SESSIONS
        assert get_ledger(ids[0]) is None
        assert get_ledger(ids[-1]) is not None
    finally:
        for browser_session_id in ids:
            discard_session_v2(browser_session_id)
            stop_ledger(browser_session_id)


@pytest.mark.asyncio
async def test_reconnect_reuses_the_session_and_re_emits_a_snapshot() -> None:
    first: list[RecordingUpdateV2] = []
    session = _start(first)
    ledger = get_ledger(PBS_ID)
    assert ledger is not None
    ledger.append(_gesture(0, "mouse_pressed", x=1, y=2, button="left", click_count=1, tag="A"))
    session.interpret()

    second: list[RecordingUpdateV2] = []
    reconnected = _start(second)

    assert reconnected is session
    assert second[-1].is_snapshot is True
    assert [step.title for step in second[-1].steps] == ["Click a"]
    assert second[-1].session_revision > first[-1].session_revision
