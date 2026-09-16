from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.client.types.workflow_definition_yaml_blocks_item import WorkflowDefinitionYamlBlocksItem_Wait
from skyvern.forge import app
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEvent as StreamingExfiltratedEvent
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import (
    ExfiltratedEventSource as StreamingExfiltratedEventSource,
)
from skyvern.services.browser_recording.interpretation import (
    RecordingInterpretationSession,
    streaming_events_to_recording_events,
)
from skyvern.services.browser_recording.service import Processor
from skyvern.services.browser_recording.types import (
    ActionKind,
    ActionTarget,
    ActionWait,
    ExfiltratedConsoleEvent,
    Mouse,
    RecordingDraftStep,
    RecordingDraftStepStatus,
    RecordingInterpretationUpdate,
)

ORG_ID = "org_123"
PBS_ID = "pbs_123"
WP_ID = "wpid_123"


def test_streaming_console_event_reifies_for_recording_processor() -> None:
    event = StreamingExfiltratedEvent(
        event_name="user_interaction",
        source=StreamingExfiltratedEventSource.CONSOLE,
        timestamp=1234.0,
        params={
            "type": "click",
            "url": "https://example.com",
            "timestamp": 1234.0,
            "target": {
                "tagName": "BUTTON",
                "id": "submit",
                "text": ["Submit"],
                "skyId": "sky-1",
            },
            "mousePosition": {"xp": 0.5, "yp": 0.5},
            "activeElement": {"tagName": "BUTTON"},
            "window": {
                "height": 800,
                "width": 1200,
                "scrollX": 0,
                "scrollY": 0,
            },
        },
    )

    reified = streaming_events_to_recording_events([event])

    assert len(reified) == 1
    assert isinstance(reified[0], ExfiltratedConsoleEvent)
    assert reified[0].params.target.skyId == "sky-1"


def _click_streaming_event(
    *,
    timestamp: float = 1234.0,
    capture_seq: int = -1,
    sky_id: str = "sky-1",
    target_id: str = "submit",
) -> StreamingExfiltratedEvent:
    return StreamingExfiltratedEvent(
        event_name="user_interaction",
        source=StreamingExfiltratedEventSource.CONSOLE,
        timestamp=timestamp,
        capture_seq=capture_seq,
        params={
            "type": "click",
            "url": "https://example.com",
            "timestamp": timestamp,
            "target": {
                "tagName": "BUTTON",
                "id": target_id,
                "text": ["Submit"],
                "skyId": sky_id,
                "selector": f"#{target_id}",
                "accessibleName": target_id,
            },
            "mousePosition": {"xp": 0.5, "yp": 0.5},
            "activeElement": {"tagName": "BUTTON"},
            "window": {
                "height": 800,
                "width": 1200,
                "scrollX": 0,
                "scrollY": 0,
            },
        },
    )


@pytest.mark.asyncio
async def test_live_interpretation_drops_inferred_waits() -> None:
    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
    )
    first_focus = _click_streaming_event(timestamp=1000.0, capture_seq=0)
    first_focus.params["type"] = "focus"
    first_focus.timestamp = 1.0
    second_focus = _click_streaming_event(timestamp=7000.0, capture_seq=2)
    second_focus.params["type"] = "focus"
    second_focus.timestamp = 7.0
    session.ingest_events(
        [
            first_focus,
            StreamingExfiltratedEvent(
                event_name="net:activity",
                source=StreamingExfiltratedEventSource.CDP,
                timestamp=6.5,
                capture_seq=1,
                params={"count": 3},
            ),
            second_focus,
        ]
    )

    steps = await session.flush()

    assert steps == []
    assert session.recorded_actions() == []


@pytest.mark.asyncio
async def test_jittered_reclick_yields_single_draft_step(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        return {"block_label": "click_submit", "title": "Click Submit", "prompt": "Click the submit button."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
    )

    session.ingest_events([_click_streaming_event(timestamp=1000.0, capture_seq=0)])
    session.ingest_events([_click_streaming_event(timestamp=1002.0, capture_seq=1)])
    steps = await session.flush()

    assert len(steps) == 1


@pytest.mark.asyncio
async def test_live_enrichment_carries_recording_correlation_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"block_label": "click_submit", "title": "Click Submit", "prompt": "Click submit."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
        recording_attempt_id="attempt-1",
    )

    session.ingest_events([_click_streaming_event(timestamp=1000.0)])
    await session.flush()

    assert len(calls) == 1
    assert calls[0]["recording_attempt_id"] == "attempt-1"
    assert calls[0]["interpretation_session_id"] == session.interpretation_session_id


@pytest.mark.asyncio
async def test_non_adjacent_duplicate_suppressed_but_later_repeat_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        return {"block_label": "click", "title": "Click", "prompt": "Click."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
    )

    session.ingest_events(
        [
            _click_streaming_event(timestamp=1000.0, capture_seq=0, sky_id="sky-a", target_id="a"),
            _click_streaming_event(timestamp=1010.0, capture_seq=1, sky_id="sky-b", target_id="b"),
            _click_streaming_event(timestamp=1005.0, capture_seq=2, sky_id="sky-a", target_id="a"),
        ]
    )
    steps = await session.flush()

    assert [(step.action_kind, step.timestamp_start) for step in steps] == [
        (ActionKind.CLICK, 1000.0),
        (ActionKind.CLICK, 1010.0),
    ]

    # A genuine later repeat of A (well outside the dedup window) is preserved.
    session.ingest_events([_click_streaming_event(timestamp=5000.0, capture_seq=3, sky_id="sky-a", target_id="a")])
    steps = await session.flush()

    assert len(steps) == 3
    assert steps[-1].timestamp_start == 5000.0


@pytest.mark.asyncio
async def test_ingest_events_sorts_unprocessed_tail_by_capture_seq() -> None:
    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        debounce_seconds=60,
    )

    # Events arrive out of capture order (later capture_seq first), as can happen
    # when a console event's async materialization completes after a later event.
    session.ingest_events(
        [
            _click_streaming_event(timestamp=1003.0, capture_seq=3, sky_id="sky-c", target_id="c"),
            _click_streaming_event(timestamp=1001.0, capture_seq=1, sky_id="sky-a", target_id="a"),
            _click_streaming_event(timestamp=1002.0, capture_seq=2, sky_id="sky-b", target_id="b"),
        ]
    )

    assert [event.capture_seq for event in session.events] == [1, 2, 3]
    session.cancel()


@pytest.mark.asyncio
async def test_recording_interpretation_session_reschedules_debounce_on_new_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpret_calls = 0

    async def fake_interpret(self: RecordingInterpretationSession, *, finalized: bool) -> None:
        nonlocal interpret_calls
        interpret_calls += 1
        self.pending = False
        self.finalized = finalized

    monkeypatch.setattr(RecordingInterpretationSession, "_interpret", fake_interpret)

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        debounce_seconds=60,
    )
    session.ingest_events([_click_streaming_event()])
    first_task = session._debounce_task
    session.ingest_events([_click_streaming_event(timestamp=1235.0)])
    await asyncio.sleep(0)

    assert first_task is not None
    assert first_task.cancelled() or first_task.cancelling()
    assert session._debounce_task is not None
    assert session._debounce_task is not first_task
    assert interpret_calls == 0

    session.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_recording_interpretation_session_cancel_clears_debounce_task() -> None:
    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        debounce_seconds=60,
    )
    session.ingest_events([_click_streaming_event()])

    assert session._debounce_task is not None
    session.cancel()
    assert session._debounce_task is None
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_recording_interpretation_session_flush_cancels_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    debounce_started = asyncio.Event()
    allow_debounce_finish = asyncio.Event()

    async def fake_debounced_interpret(self: RecordingInterpretationSession, delay: float) -> None:
        debounce_started.set()
        await allow_debounce_finish.wait()

    monkeypatch.setattr(RecordingInterpretationSession, "_debounced_interpret", fake_debounced_interpret)

    flush_calls = 0

    async def fake_interpret(self: RecordingInterpretationSession, *, finalized: bool) -> None:
        nonlocal flush_calls
        flush_calls += 1
        self.pending = False
        self.finalized = finalized

    monkeypatch.setattr(RecordingInterpretationSession, "_interpret", fake_interpret)

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
    )
    session.ingest_events([_click_streaming_event()])
    await debounce_started.wait()

    await session.flush()

    assert flush_calls == 1
    assert session._debounce_task is None


@pytest.mark.asyncio
async def test_recording_interpretation_session_advances_past_unhandled_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_action = ActionWait(
        kind=ActionKind.WAIT,
        target=ActionTarget(mouse=Mouse(xp=None, yp=None)),
        timestamp_start=1000.0,
        timestamp_end=8000.0,
        url="https://example.com",
        duration_ms=7000,
    )
    unhandled_action = MagicMock()
    unhandled_action.kind = "unsupported"

    processor = MagicMock()
    processor.create_wait_block = AsyncMock(
        return_value=WorkflowDefinitionYamlBlocksItem_Wait(label="wait_7s", wait_sec=7),
    )
    monkeypatch.setattr(
        "skyvern.services.browser_recording.interpretation.Processor",
        lambda *args, **kwargs: processor,
    )

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
    )
    session.events = [MagicMock(), MagicMock()]
    session._processed_event_count = len(session.events)
    session._all_actions = [wait_action, unhandled_action]

    await session._interpret(finalized=False)

    assert session.emitted_action_count == 2
    assert len(session.steps) == 1


@pytest.mark.asyncio
async def test_enrichment_calls_are_capped_by_semaphore(monkeypatch: pytest.MonkeyPatch) -> None:
    in_flight = 0
    max_in_flight = 0

    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return {"block_label": "click_x", "title": "Click X", "prompt": "Click X."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
    )
    session._enrichment_semaphore = asyncio.Semaphore(2)

    events = [
        _click_streaming_event(timestamp=1000.0 + i, capture_seq=i, sky_id=f"sky-{i}", target_id=f"t{i}")
        for i in range(8)
    ]
    session.ingest_events(events)
    steps = await session.flush()

    assert len(steps) == 8
    assert all(step.status == RecordingDraftStepStatus.READY for step in steps)
    assert max_in_flight == 2


def test_emit_snapshot_replays_current_revision_without_incrementing() -> None:
    updates: list[int] = []

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda update: updates.append(update.session_revision),
    )
    session.session_revision = 2
    session.steps = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_submit",
            title="Click submit",
            navigation_goal="Click submit",
        )
    ]

    session.emit_snapshot()

    assert updates == [2]
    assert session.session_revision == 2


def test_start_session_resumes_existing_interpretation_session() -> None:
    from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

    registry = RecordingInterpretationSessionRegistry()
    first_updates: list[int] = []
    second_updates: list[int] = []

    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda update: first_updates.append(update.session_revision),
    )
    session = registry._sessions[PBS_ID]
    session.session_revision = 3
    session.steps = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_submit",
            title="Click submit",
            navigation_goal="Click submit",
        )
    ]

    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda update: second_updates.append(update.session_revision),
    )

    assert registry._sessions[PBS_ID] is session
    assert first_updates == []
    assert second_updates == [3]


def test_start_session_resumes_after_websocket_disconnect_without_stop() -> None:
    from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

    registry = RecordingInterpretationSessionRegistry()
    reconnect_updates: list[int] = []

    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
    )
    session = registry._sessions[PBS_ID]
    session.session_revision = 4
    session.steps = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_submit",
            title="Click submit",
            navigation_goal="Click submit",
        )
    ]

    # WebSocket loop teardown no longer calls stop_session; only end-exfiltration does.
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda update: reconnect_updates.append(update.session_revision),
    )

    assert registry._sessions[PBS_ID] is session
    assert reconnect_updates == [4]


def test_start_session_same_recording_attempt_id_reuses_session() -> None:
    from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

    registry = RecordingInterpretationSessionRegistry()
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        recording_attempt_id="attempt-1",
    )
    session = registry._sessions[PBS_ID]
    session.session_revision = 5

    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        recording_attempt_id="attempt-1",
    )

    # Same recording (reconnect) reuses the cached session and its revision.
    assert registry._sessions[PBS_ID] is session
    assert registry._sessions[PBS_ID].session_revision == 5


def test_start_session_new_recording_attempt_id_continues_unfinished_session() -> None:
    """SKY-12429: a new attempt id on an unfinished recording continues it.

    The client mints the attempt id per recording and keeps it stable across
    reconnects, so a different id on the same unfinished session means the client
    lost its in-memory state (e.g. page reload). The accumulated drafts must be
    carried forward and resynced to the reconnecting client, not wiped.
    """
    from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

    registry = RecordingInterpretationSessionRegistry()
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        recording_attempt_id="attempt-1",
    )
    session = registry._sessions[PBS_ID]
    session.session_revision = 42
    session.steps = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_submit",
        )
    ]

    resynced: list[RecordingInterpretationUpdate] = []
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=resynced.append,
        recording_attempt_id="attempt-2",
    )

    # Same session, drafts intact, the new attempt id adopted, and the
    # reconnecting client resynced with the accumulated steps.
    continued = registry._sessions[PBS_ID]
    assert continued is session
    assert continued.recording_attempt_id == "attempt-2"
    assert [s.step_id for s in continued.steps] == ["step-1"]
    assert resynced and [s.step_id for s in resynced[-1].steps] == ["step-1"]


def test_start_session_after_finalized_recording_starts_fresh() -> None:
    """Done/Discard finalize and pop the session; a lingering finalized session
    must not leak its steps into the next recording."""
    from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

    registry = RecordingInterpretationSessionRegistry()
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        recording_attempt_id="attempt-1",
    )
    finalized = registry._sessions[PBS_ID]
    finalized.finalized = True
    finalized.steps = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_submit",
        )
    ]

    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        recording_attempt_id="attempt-2",
    )

    fresh = registry._sessions[PBS_ID]
    assert fresh is not finalized
    assert fresh.steps == []


@pytest.mark.asyncio
async def test_emits_deltas_for_steps_and_snapshot_on_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        return {"block_label": "click_submit", "title": "Click Submit", "prompt": "Click submit."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    updates: list[RecordingInterpretationUpdate] = []
    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=updates.append,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
        deltas_enabled=True,
    )

    session.ingest_events([_click_streaming_event(timestamp=1000.0)])
    await session.flush()

    # Steps arrive as deltas (placeholder + enriched), never re-sending the full list.
    deltas = [u for u in updates if not u.is_snapshot]
    assert any(u.changed_steps for u in deltas)
    assert all(u.steps == [] for u in deltas)

    # Finalize ends with an authoritative snapshot carrying the full list.
    assert updates[-1].is_snapshot is True
    assert updates[-1].finalized is True
    assert len(updates[-1].steps) == 1

    # A delta never smuggles the whole growing list back in.
    assert all(u.is_snapshot or not u.steps for u in updates)


@pytest.mark.asyncio
async def test_no_deltas_when_client_lacks_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        return {"block_label": "click", "title": "Click", "prompt": "Click."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    updates: list[RecordingInterpretationUpdate] = []
    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=updates.append,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
        # deltas_enabled defaults False — a client that didn't opt in gets snapshots.
    )

    session.ingest_events([_click_streaming_event(timestamp=1000.0)])
    await session.flush()

    # Every update is a full snapshot; no changed_steps are ever sent.
    assert all(u.is_snapshot for u in updates)
    assert all(not u.changed_steps for u in updates)
    assert updates[-1].steps  # final snapshot still carries the steps


@pytest.mark.asyncio
async def test_resume_capture_emits_resync_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        return {"block_label": "click", "title": "Click", "prompt": "Click."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    updates: list[RecordingInterpretationUpdate] = []
    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=updates.append,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
    )

    session.ingest_events([_click_streaming_event(timestamp=1000.0)])
    await asyncio.sleep(0.05)

    session.pause_capture()
    updates.clear()
    session.resume_capture()

    assert len(updates) == 1
    assert updates[0].is_snapshot is True
    session.cancel()


@pytest.mark.asyncio
async def test_new_attempt_id_mid_recording_continues_session_and_keeps_drafts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SKY-12429: a reconnect with a new attempt id continues the same recording.

    The client only presents a different recording_attempt_id for an unfinished
    recording when it lost its in-memory state (e.g. page reload). The registry
    must continue the populated session: resync the panel with the accumulated
    drafts instead of blanking it, keep interpreting new events, and let the
    finished recording retain everything captured.
    """
    from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        return {"block_label": "click_submit", "title": "Click Submit", "prompt": "Click the submit button."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    registry = RecordingInterpretationSessionRegistry()
    panel: list[RecordingInterpretationUpdate] = []

    # Attempt 1: the user interacts and drafts accumulate.
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=panel.append,
        recording_attempt_id="attempt-1",
    )
    session_one = registry._sessions[PBS_ID]
    registry.ingest_events(
        PBS_ID,
        [
            _click_streaming_event(timestamp=1000.0, capture_seq=0, sky_id="sky-a", target_id="a"),
            _click_streaming_event(timestamp=1010.0, capture_seq=1, sky_id="sky-b", target_id="b"),
        ],
    )
    await session_one._interpret(finalized=False)

    # The panel shows those drafts (full snapshot with a non-empty step list).
    populated_snapshots = [u for u in panel if u.is_snapshot and u.steps]
    assert populated_snapshots, "expected the panel to display the interpreted drafts"
    accumulated_step_count = len(session_one.steps)
    accumulated_step_ids = {step.step_id for step in session_one.steps}
    assert accumulated_step_count >= 1

    # A reconnect arrives with a NEW attempt id (same browser session, not finalized).
    panel.clear()
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=panel.append,
        recording_attempt_id="attempt-2",
    )
    session_two = registry._sessions[PBS_ID]

    # The recording continues: same session, drafts intact, new attempt id adopted,
    # and the reconnecting client immediately resynced with the accumulated steps.
    assert session_two is session_one
    assert len(session_two.steps) == accumulated_step_count
    assert session_two.recording_attempt_id == "attempt-2"
    assert panel and panel[-1].is_snapshot and len(panel[-1].steps) == accumulated_step_count

    # New interactions after the reconnect keep extending the same draft list.
    registry.ingest_events(
        PBS_ID,
        [_click_streaming_event(timestamp=2000.0, capture_seq=2, sky_id="sky-c", target_id="c")],
    )
    await session_two._interpret(finalized=False)
    assert len(session_two.steps) > accumulated_step_count

    assert accumulated_step_ids < {step.step_id for step in session_two.steps}

    drafts = await registry.stop_session(PBS_ID)
    assert (
        registry.get_finalized_actions(
            interpretation_session_id=session_two.interpretation_session_id,
            browser_session_id=PBS_ID,
            organization_id="other-org",
            workflow_permanent_id=WP_ID,
        )
        is None
    )
    actions = registry.get_finalized_actions(
        interpretation_session_id=session_two.interpretation_session_id,
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
    )
    blocks, _, _ = await Processor(PBS_ID, ORG_ID, WP_ID).process(
        [],
        draft_steps=drafts,
        recorded_actions=actions,
    )
    code = "\n".join(block.code for block in blocks)
    assert "#a" in code
    assert "#b" in code
    assert "#c" in code

    registry.discard_finalized_actions(session_two.interpretation_session_id)
    assert (
        registry.get_finalized_actions(
            interpretation_session_id=session_two.interpretation_session_id,
            browser_session_id=PBS_ID,
            organization_id=ORG_ID,
            workflow_permanent_id=WP_ID,
        )
        is None
    )

    registry.discard_session(PBS_ID)


@pytest.mark.asyncio
async def test_process_recording_discards_finalized_actions_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_routes
    from skyvern.schemas.browser_sessions import ProcessBrowserSessionRecordingRequest

    registry = MagicMock()
    registry.get_finalized_actions.return_value = [MagicMock()]
    monkeypatch.setattr(browser_sessions_routes, "interpretation_registry", registry)

    persistent_sessions_manager = MagicMock()
    persistent_sessions_manager.get_session = AsyncMock(return_value=MagicMock())
    recording_service = MagicMock()
    recording_service.process_recording = AsyncMock(return_value=([], [], None, None))
    route_app = MagicMock(
        PERSISTENT_SESSIONS_MANAGER=persistent_sessions_manager,
        BROWSER_SESSION_RECORDING_SERVICE=recording_service,
        AGENT_FUNCTION=MagicMock(validate_code_block=AsyncMock()),
    )
    monkeypatch.setattr(browser_sessions_routes, "app", route_app)

    await browser_sessions_routes.process_recording(
        browser_session_id=PBS_ID,
        recording_request=ProcessBrowserSessionRecordingRequest(
            workflow_permanent_id=WP_ID,
            interpretation_session_id="interpretation-1",
        ),
        current_org=MagicMock(organization_id=ORG_ID),
    )

    registry.discard_finalized_actions.assert_called_once_with("interpretation-1")


@pytest.mark.asyncio
async def test_process_recording_retains_finalized_actions_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_routes
    from skyvern.schemas.browser_sessions import ProcessBrowserSessionRecordingRequest

    registry = MagicMock()
    registry.get_finalized_actions.return_value = [MagicMock()]
    monkeypatch.setattr(browser_sessions_routes, "interpretation_registry", registry)

    persistent_sessions_manager = MagicMock()
    persistent_sessions_manager.get_session = AsyncMock(return_value=MagicMock())
    recording_service = MagicMock()
    recording_service.process_recording = AsyncMock(side_effect=RuntimeError("processing failed"))
    route_app = MagicMock(
        PERSISTENT_SESSIONS_MANAGER=persistent_sessions_manager,
        BROWSER_SESSION_RECORDING_SERVICE=recording_service,
        AGENT_FUNCTION=MagicMock(validate_code_block=AsyncMock()),
    )
    monkeypatch.setattr(browser_sessions_routes, "app", route_app)

    with pytest.raises(RuntimeError, match="processing failed"):
        await browser_sessions_routes.process_recording(
            browser_session_id=PBS_ID,
            recording_request=ProcessBrowserSessionRecordingRequest(
                workflow_permanent_id=WP_ID,
                interpretation_session_id="interpretation-1",
            ),
            current_org=MagicMock(organization_id=ORG_ID),
        )

    registry.discard_finalized_actions.assert_not_called()


@pytest.mark.asyncio
async def test_process_recording_requires_code_block_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.exceptions import DisabledBlockExecutionError
    from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_routes
    from skyvern.schemas.browser_sessions import ProcessBrowserSessionRecordingRequest

    persistent_sessions_manager = MagicMock()
    persistent_sessions_manager.get_session = AsyncMock(return_value=MagicMock())
    recording_service = MagicMock()
    recording_service.process_recording = AsyncMock()
    agent_function = MagicMock()
    agent_function.validate_code_block = AsyncMock(side_effect=DisabledBlockExecutionError("CodeBlock is disabled"))
    monkeypatch.setattr(
        browser_sessions_routes,
        "app",
        MagicMock(
            PERSISTENT_SESSIONS_MANAGER=persistent_sessions_manager,
            BROWSER_SESSION_RECORDING_SERVICE=recording_service,
            AGENT_FUNCTION=agent_function,
        ),
    )

    with pytest.raises(DisabledBlockExecutionError, match="CodeBlock is disabled"):
        await browser_sessions_routes.process_recording(
            browser_session_id=PBS_ID,
            recording_request=ProcessBrowserSessionRecordingRequest(workflow_permanent_id=WP_ID),
            current_org=MagicMock(organization_id=ORG_ID),
        )

    recording_service.process_recording.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_recording_waits_for_late_finalized_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_routes
    from skyvern.schemas.browser_sessions import ProcessBrowserSessionRecordingRequest

    finalized_actions = [MagicMock()]
    registry = MagicMock()
    registry.get_finalized_actions.side_effect = [None, finalized_actions]
    registry.stop_session = AsyncMock(return_value=[])
    monkeypatch.setattr(browser_sessions_routes, "interpretation_registry", registry)

    recording_service = MagicMock()
    recording_service.process_recording = AsyncMock(return_value=([], [], None, None))
    monkeypatch.setattr(
        browser_sessions_routes,
        "app",
        MagicMock(
            PERSISTENT_SESSIONS_MANAGER=MagicMock(get_session=AsyncMock(return_value=MagicMock())),
            BROWSER_SESSION_RECORDING_SERVICE=recording_service,
            AGENT_FUNCTION=MagicMock(validate_code_block=AsyncMock()),
        ),
    )

    await browser_sessions_routes.process_recording(
        browser_session_id=PBS_ID,
        recording_request=ProcessBrowserSessionRecordingRequest(
            workflow_permanent_id=WP_ID,
            interpretation_session_id="interpretation-1",
        ),
        current_org=MagicMock(organization_id=ORG_ID),
    )

    registry.stop_session.assert_awaited_once_with(PBS_ID)
    assert recording_service.process_recording.await_args.kwargs["recorded_actions"] == finalized_actions
    registry.discard_finalized_actions.assert_called_once_with("interpretation-1")


@pytest.mark.asyncio
async def test_concurrent_stop_session_waits_for_the_same_flush() -> None:
    from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

    registry = RecordingInterpretationSessionRegistry()
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _update: None,
    )
    session = registry._sessions[PBS_ID]
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()

    async def delayed_flush() -> list[RecordingDraftStep]:
        flush_started.set()
        await release_flush.wait()
        return []

    session.flush = AsyncMock(side_effect=delayed_flush)
    first = asyncio.create_task(registry.stop_session(PBS_ID))
    await flush_started.wait()
    second = asyncio.create_task(registry.stop_session(PBS_ID))
    await asyncio.sleep(0)

    assert not second.done()
    release_flush.set()
    assert await first == []
    assert await second == []
    session.flush.assert_awaited_once()
