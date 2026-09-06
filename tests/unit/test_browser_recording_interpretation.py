from __future__ import annotations

import asyncio
import typing as t
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
from skyvern.services.browser_recording.service import Processor, bound_credential_ids
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
    finished recording build blocks from everything captured.
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

    # Finishing builds blocks from everything captured across the reconnect.
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    blocks = processor.drafts_to_blocks(session_two.steps)
    assert len(blocks) == len(session_two.steps)

    registry.discard_session(PBS_ID)


def test_drafts_to_blocks_collapses_credentialed_login_form_into_one_login_block() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    login_url = "https://example.com/login"
    drafts = [
        RecordingDraftStep(
            step_id="step-goto",
            action_kind=ActionKind.URL_CHANGE,
            block_type="goto_url",
            label="goto",
            url=login_url,
        ),
        RecordingDraftStep(
            step_id="step-email",
            action_kind=ActionKind.INPUT_TEXT,
            block_type="action",
            label="type_email",
            url=login_url,
            parameters=[{"key": "email"}],
            parameter_keys=["email"],
        ),
        RecordingDraftStep(
            step_id="step-password",
            action_kind=ActionKind.INPUT_TEXT,
            block_type="action",
            label="type_password",
            title="Log into example",
            url=login_url,
            credential_kind="password",
            credential_id="cred_abc",
        ),
        RecordingDraftStep(
            step_id="step-submit",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_sign_in",
            url=login_url,
        ),
        RecordingDraftStep(
            step_id="step-after",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_dashboard",
            url="https://example.com/dashboard",
        ),
    ]

    blocks = processor.drafts_to_blocks(drafts)
    parameters = processor.blocks_to_parameters(blocks, bound_credential_ids(drafts))

    assert [block.block_type for block in blocks] == ["goto_url", "login", "action"]
    login_block = blocks[1]
    assert login_block.url == login_url
    assert login_block.title == "Log into example"
    # Keyed by the credential id: a token the editor swaps for a real key when it applies the blocks.
    assert login_block.parameter_keys == ["cred_abc"]
    assert login_block.parameters == [{"key": "cred_abc"}]
    # The email typing and the submit click are the login block's job now.
    assert blocks[2].label == "click_dashboard"

    credential_parameters = [p for p in parameters if p.parameter_type == "credential"]
    assert [(p.key, p.credential_id) for p in credential_parameters] == [("cred_abc", "cred_abc")]
    # The absorbed email step's placeholder parameter goes with it.
    assert [p.key for p in parameters if p.parameter_type == "workflow"] == []


def test_drafts_to_blocks_points_a_secret_fill_at_the_credential_field() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        RecordingDraftStep(
            step_id="step-secret",
            action_kind=ActionKind.INPUT_TEXT,
            block_type="action",
            label="type_api_token",
            url="https://example.com/settings",
            navigation_goal="Type 'API token' with {{ api_token }}.",
            parameters=[{"key": "api_token"}],
            parameter_keys=["api_token"],
            credential_kind="secret",
            credential_id="cred_secret",
        ),
    ]

    blocks = processor.drafts_to_blocks(drafts)
    parameters = processor.blocks_to_parameters(blocks, bound_credential_ids(drafts))

    assert [block.block_type for block in blocks] == ["action"]
    # The instruction reads the credential field, not the empty placeholder it replaced.
    assert blocks[0].navigation_goal == "Type 'API token' with {{ cred_secret.secret_value }}."
    assert blocks[0].parameter_keys == ["cred_secret"]
    assert blocks[0].parameters == [{"key": "cred_secret"}]
    assert [(p.parameter_type, p.key) for p in parameters] == [("credential", "cred_secret")]


def test_drafts_to_blocks_leaves_a_card_fill_unbound() -> None:
    """A card credential spans several fields and the recorder does not say which one was typed.

    Attaching it anyway would emit a credential parameter nothing references while the
    instruction still rendered the empty placeholder.
    """
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        RecordingDraftStep(
            step_id="step-card",
            action_kind=ActionKind.INPUT_TEXT,
            block_type="action",
            label="type_card_number",
            url="https://example.com/checkout",
            navigation_goal="Type 'Card number' with {{ cardnumber }}.",
            parameters=[{"key": "cardnumber"}],
            parameter_keys=["cardnumber"],
            credential_kind="credit_card",
            credential_id="cred_card",
        ),
    ]

    blocks = processor.drafts_to_blocks(drafts)
    parameters = processor.blocks_to_parameters(blocks, bound_credential_ids(drafts))

    assert [block.block_type for block in blocks] == ["action"]
    assert blocks[0].navigation_goal == "Type 'Card number' with {{ cardnumber }}."
    assert blocks[0].parameter_keys == ["cardnumber"]
    assert [(p.parameter_type, p.key) for p in parameters] == [("workflow", "cardnumber")]


def test_drafts_to_blocks_keeps_post_login_steps_when_the_url_never_changes() -> None:
    """A modal/SPA login must not swallow the rest of the session.

    The whole recording sits on one URL, so a same-URL span reaches to the last step.
    """
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    app_url = "https://example.com/app"
    drafts = [
        RecordingDraftStep(
            step_id="step-goto",
            action_kind=ActionKind.URL_CHANGE,
            block_type="goto_url",
            label="goto",
            url=app_url,
        ),
        RecordingDraftStep(
            step_id="step-open-modal",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_sign_in_link",
            url=app_url,
        ),
        RecordingDraftStep(
            step_id="step-email",
            action_kind=ActionKind.INPUT_TEXT,
            block_type="action",
            label="type_email",
            url=app_url,
            parameters=[{"key": "email"}],
            parameter_keys=["email"],
        ),
        RecordingDraftStep(
            step_id="step-password",
            action_kind=ActionKind.INPUT_TEXT,
            block_type="action",
            label="type_password",
            url=app_url,
            credential_kind="password",
            credential_id="cred_abc",
        ),
        RecordingDraftStep(
            step_id="step-submit",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_submit",
            url=app_url,
        ),
        RecordingDraftStep(
            step_id="step-after-1",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_new_invoice",
            url=app_url,
        ),
        RecordingDraftStep(
            step_id="step-after-2",
            action_kind=ActionKind.INPUT_TEXT,
            block_type="action",
            label="type_amount",
            url=app_url,
            parameters=[{"key": "amount"}],
            parameter_keys=["amount"],
        ),
        RecordingDraftStep(
            step_id="step-after-3",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click_save",
            url=app_url,
        ),
    ]

    blocks = processor.drafts_to_blocks(drafts)
    parameters = processor.blocks_to_parameters(blocks, bound_credential_ids(drafts))

    assert [block.label for block in blocks] == [
        "goto",
        "click_sign_in_link",
        "type_password",
        "click_new_invoice",
        "type_amount",
        "click_save",
    ]
    assert [block.block_type for block in blocks] == [
        "goto_url",
        "action",
        "login",
        "action",
        "action",
        "action",
    ]
    # The post-login work keeps its own parameters.
    assert sorted(p.key for p in parameters if p.parameter_type == "workflow") == ["amount"]


def _step(step_id: str, kind: ActionKind, label: str, **kwargs: t.Any) -> RecordingDraftStep:
    return RecordingDraftStep(
        step_id=step_id,
        action_kind=kind,
        block_type=kwargs.pop("block_type", "action"),
        label=label,
        **kwargs,
    )


def test_drafts_to_blocks_keeps_the_work_between_a_login_and_a_later_reauth() -> None:
    """The same credential used twice is two logins, not one span over everything between."""
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        _step("s1", ActionKind.INPUT_TEXT, "type_password_1", credential_kind="password", credential_id="cred_abc"),
        _step("s2", ActionKind.CLICK, "click_submit_1"),
        _step("s3", ActionKind.URL_CHANGE, "goto_dashboard", block_type="goto_url", url="https://example.com/app"),
        _step("s4", ActionKind.CLICK, "click_billing"),
        _step("s5", ActionKind.INPUT_TEXT, "type_amount", parameters=[{"key": "amount"}], parameter_keys=["amount"]),
        _step("s6", ActionKind.URL_CHANGE, "goto_reauth", block_type="goto_url", url="https://example.com/reauth"),
        _step("s7", ActionKind.INPUT_TEXT, "type_password_2", credential_kind="password", credential_id="cred_abc"),
        _step("s8", ActionKind.CLICK, "click_submit_2"),
    ]

    blocks = processor.drafts_to_blocks(drafts)

    assert [block.label for block in blocks] == [
        "type_password_1",
        "goto_dashboard",
        "click_billing",
        "type_amount",
        "goto_reauth",
        "type_password_2",
    ]
    assert [block.block_type for block in blocks] == [
        "login",
        "goto_url",
        "action",
        "action",
        "goto_url",
        "login",
    ]


def test_drafts_to_blocks_absorbs_only_the_field_next_to_the_credential() -> None:
    """A signup-style form's earlier fields are not the login block's to type."""
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        _step("s1", ActionKind.INPUT_TEXT, "type_first_name", parameters=[{"key": "first"}], parameter_keys=["first"]),
        _step("s2", ActionKind.INPUT_TEXT, "type_last_name", parameters=[{"key": "last"}], parameter_keys=["last"]),
        _step("s3", ActionKind.INPUT_TEXT, "type_email", parameters=[{"key": "email"}], parameter_keys=["email"]),
        _step("s4", ActionKind.INPUT_TEXT, "type_password", credential_kind="password", credential_id="cred_abc"),
    ]

    blocks = processor.drafts_to_blocks(drafts)
    parameters = processor.blocks_to_parameters(blocks, bound_credential_ids(drafts))

    # Only the email field next to the password is absorbed; the name fields survive.
    assert [block.label for block in blocks] == ["type_first_name", "type_last_name", "type_password"]
    assert [block.block_type for block in blocks] == ["action", "action", "login"]
    assert sorted(p.key for p in parameters if p.parameter_type == "workflow") == ["first", "last"]


def test_drafts_to_blocks_keeps_in_app_work_between_two_uses_of_one_credential() -> None:
    """A re-auth on a single-page app is a second login, not one span over the work between.

    Nothing navigates and every step in between is a click or a text entry, which is exactly
    what ordinary app work looks like - so adjacency, not step kind, has to bound the group.
    """
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        _step("s1", ActionKind.INPUT_TEXT, "type_password_1", credential_kind="password", credential_id="cred_abc"),
        _step("s2", ActionKind.CLICK, "click_submit_1"),
        _step("s3", ActionKind.CLICK, "click_invoices"),
        _step("s4", ActionKind.INPUT_TEXT, "type_amount", parameters=[{"key": "amount"}], parameter_keys=["amount"]),
        _step("s5", ActionKind.CLICK, "click_save"),
        _step("s6", ActionKind.CLICK, "click_reauth_prompt"),
        _step("s7", ActionKind.INPUT_TEXT, "type_password_2", credential_kind="password", credential_id="cred_abc"),
    ]

    blocks = processor.drafts_to_blocks(drafts)

    assert [block.label for block in blocks] == [
        "type_password_1",
        "click_invoices",
        "type_amount",
        "click_save",
        "click_reauth_prompt",
        "type_password_2",
    ]
    assert [block.block_type for block in blocks] == [
        "login",
        "action",
        "action",
        "action",
        "action",
        "login",
    ]


def test_drafts_to_blocks_merges_a_totp_fill_across_the_submit_click() -> None:
    """One step may separate two fills of a credential: the submit between password and code."""
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    drafts = [
        _step("s1", ActionKind.INPUT_TEXT, "type_password", credential_kind="password", credential_id="cred_abc"),
        _step("s2", ActionKind.CLICK, "click_next"),
        _step("s3", ActionKind.INPUT_TEXT, "type_code", credential_kind="totp", credential_id="cred_abc"),
        _step("s4", ActionKind.CLICK, "click_verify"),
    ]

    blocks = processor.drafts_to_blocks(drafts)

    assert [block.block_type for block in blocks] == ["login"]
    assert blocks[0].parameter_keys == ["cred_abc"]
