from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.client.types.workflow_definition_yaml_blocks_item import WorkflowDefinitionYamlBlocksItem_Wait
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


def _click_streaming_event(*, timestamp: float = 1234.0) -> StreamingExfiltratedEvent:
    return StreamingExfiltratedEvent(
        event_name="user_interaction",
        source=StreamingExfiltratedEventSource.CONSOLE,
        timestamp=timestamp,
        params={
            "type": "click",
            "url": "https://example.com",
            "timestamp": timestamp,
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
