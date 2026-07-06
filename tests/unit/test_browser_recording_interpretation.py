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


def test_drafts_to_blocks_preserves_action_parameters_and_sanitizes_duplicate_labels() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    parameter = {
        "key": "customer_name",
        "workflow_parameter_type": "string",
        "default_value": "",
        "description": "",
    }
    drafts = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="123 Submit!",
            title="Submit form",
            navigation_goal="Click submit",
            parameters=[parameter],
            parameter_keys=["customer_name"],
        ),
        RecordingDraftStep(
            step_id="step-2",
            action_kind=ActionKind.INPUT_TEXT,
            block_type="action",
            label="123 Submit!",
            title="Type name",
            navigation_goal="Type the customer name",
            parameters=[parameter],
            parameter_keys=["customer_name"],
        ),
    ]

    blocks = processor.drafts_to_blocks(drafts)
    parameters = processor.blocks_to_parameters(blocks)

    assert [block.label for block in blocks] == ["act_123_Submit", "act_123_Submit_0"]
    assert blocks[0].parameters == [parameter]
    assert blocks[0].parameter_keys == ["customer_name"]
    assert [parameter.key for parameter in parameters] == ["customer_name"]


def test_drafts_to_blocks_skips_empty_goto_url() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.URL_CHANGE,
            block_type="goto_url",
            label="visit",
            url="",
        )
    ]

    assert processor.drafts_to_blocks(drafts) == []


def test_drafts_to_blocks_goto_url_label_follows_edited_title_and_url() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.URL_CHANGE,
            block_type="goto_url",
            label="goto_wikipedia_com",
            title="Go to wikipedia.org",
            url="https://wikipedia.org/wiki/Foo",
        )
    ]

    blocks = processor.drafts_to_blocks(drafts)

    assert len(blocks) == 1
    assert blocks[0].label == "Go_to_wikipedia_org"
    assert blocks[0].url == "https://wikipedia.org/wiki/Foo"


def test_drafts_to_blocks_goto_url_label_derives_from_url_without_title_or_label() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.URL_CHANGE,
            block_type="goto_url",
            label="",
            url="https://www.wikipedia.org/wiki/Foo",
        )
    ]

    blocks = processor.drafts_to_blocks(drafts)

    assert len(blocks) == 1
    assert blocks[0].label == "goto_www_wikipedia_org"
    assert blocks[0].url == "https://www.wikipedia.org/wiki/Foo"


def test_drafts_to_blocks_goto_url_label_preserves_edited_label_without_title() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.URL_CHANGE,
            block_type="goto_url",
            label="Open Wikipedia",
            url="https://www.wikipedia.org/wiki/Foo",
        )
    ]

    blocks = processor.drafts_to_blocks(drafts)

    assert len(blocks) == 1
    assert blocks[0].label == "Open_Wikipedia"
    assert blocks[0].url == "https://www.wikipedia.org/wiki/Foo"


@pytest.mark.asyncio
async def test_processor_process_uses_draft_steps_without_compressed_chunks() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        RecordingDraftStep(
            step_id="step-1",
            action_kind=ActionKind.WAIT,
            block_type="wait",
            label="wait",
            wait_sec=2,
        )
    ]

    blocks, parameters = await processor.process([], draft_steps=drafts)

    assert len(blocks) == 1
    assert blocks[0].block_type == "wait"
    assert blocks[0].wait_sec == 5
    assert parameters == []


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


def test_start_session_new_recording_attempt_id_forces_fresh_session() -> None:
    from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

    registry = RecordingInterpretationSessionRegistry()
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _: None,
        recording_attempt_id="attempt-1",
    )
    stale = registry._sessions[PBS_ID]
    stale.session_revision = 42
    stale.steps = [
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

    # A new recording gets a fresh session: not the stale object, revision reset,
    # no carried-over steps.
    fresh = registry._sessions[PBS_ID]
    assert fresh is not stale
    assert fresh.recording_attempt_id == "attempt-2"
    assert fresh.session_revision == 0
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
