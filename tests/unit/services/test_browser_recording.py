"""
Just an example unit test for now. Will expand later.
"""

import asyncio
import time
import typing as t

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEvent as StreamingExfiltratedEvent
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import (
    ExfiltratedEventSource as StreamingExfiltratedEventSource,
)
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import (
    ExfiltrationChannel,
)
from skyvern.services.browser_recording.interpretation import RecordingInterpretationSession
from skyvern.services.browser_recording.service import (
    Processor,
    deterministic_input_text_parameter_key,
    summarize_exfiltrated_recording_events,
)
from skyvern.services.browser_recording.types import (
    ActionInputText,
    ActionKind,
    ActionTarget,
    ActionUrlChange,
    ActionWait,
    ExfiltratedCdpEvent,
    ExfiltratedConsoleEvent,
    ExfiltratedEventCdpParams,
    Mouse,
    RecordingDraftStepStatus,
    RecordingInterpretationUpdate,
)

ORG_ID = "org_123"
PBS_ID = "pbs_123"
WP_ID = "wpid_123"


class DummyVncChannel:
    identity: t.ClassVar[dict[str, t.Any]] = {}
    browser_session: t.ClassVar[None] = None
    x_api_key: t.ClassVar[None] = None
    organization_id: t.ClassVar[str] = ORG_ID


def make_console_event(
    params: dict[str, t.Any],
    timestamp: float,
) -> ExfiltratedConsoleEvent:
    default_params = {
        "url": "https://example.com",
        "activeElement": {
            "tagName": "BUTTON",
        },
        "window": {
            "height": 800,
            "width": 1200,
            "scrollX": 0,
            "scrollY": 0,
        },
        "mousePosition": {"xp": 0.5, "yp": 0.5},
    }

    params = {**default_params, **params}

    # params.timestamp is the client clock (Date.now(), ms); the outer event
    # timestamp is the server clock (time.time(), seconds). Mirror production so
    # the Wait machine's client/server offset is ~0 for zero-skew fixtures.
    return ExfiltratedConsoleEvent(
        kind="exfiltrated-event",
        source="console",
        event_name="user_interaction",
        params=params,
        timestamp=timestamp / 1000.0,
    )


def make_mouseenter_event(
    target: dict[str, t.Any],
    timestamp: float,
) -> ExfiltratedConsoleEvent:
    params: dict[str, t.Any] = {
        "type": "mouseenter",
        "target": target,
        "timestamp": timestamp,
    }

    return make_console_event(
        params=params,
        timestamp=timestamp,
    )


def make_mouseleave_event(
    target: dict[str, t.Any],
    timestamp: float,
) -> ExfiltratedConsoleEvent:
    params: dict[str, t.Any] = {
        "type": "mouseleave",
        "target": target,
        "timestamp": timestamp,
    }

    return make_console_event(
        params=params,
        timestamp=timestamp,
    )


def make_click_event(
    target: dict[str, t.Any],
    timestamp: float,
) -> ExfiltratedConsoleEvent:
    params: dict[str, t.Any] = {
        "type": "click",
        "target": target,
        "timestamp": timestamp,
    }

    return make_console_event(
        params=params,
        timestamp=timestamp,
    )


def test_click() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])

    event = make_click_event(
        target=target,
        timestamp=1000.0,
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions([event])

    assert len(actions) == 1
    assert actions[0].kind == "click"
    assert actions[0].target.sky_id == "sky-123"


def test_identical_click_events_are_deduped() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])

    event = make_click_event(
        target=target,
        timestamp=1000.0,
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions([event, event])

    assert len(actions) == 1
    assert actions[0].kind == "click"


def test_hover() -> None:
    target = dict(id="button-1", skyId="sky-123", text=["Click me"])

    event1 = make_mouseenter_event(
        target=target,
        timestamp=1000.0,
    )

    event2 = make_mouseleave_event(
        target=target,
        timestamp=4000.0,
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions([event1, event2])

    assert len(actions) == 1


def _empty_action_target() -> ActionTarget:
    return ActionTarget(mouse=Mouse(xp=None, yp=None))


def make_streaming_console_click(timestamp_ms: float) -> StreamingExfiltratedEvent:
    return StreamingExfiltratedEvent(
        event_name="user_interaction",
        params={
            "type": "click",
            "target": {"id": "button-1", "skyId": "sky-123", "tagName": "BUTTON", "text": ["Click me"]},
            "timestamp": timestamp_ms,
            "url": "https://example.com",
            "activeElement": {"tagName": "BUTTON"},
            "window": {"height": 800, "width": 1200, "scrollX": 0, "scrollY": 0},
            "mousePosition": {"xp": 0.5, "yp": 0.5},
        },
        source=StreamingExfiltratedEventSource.CONSOLE,
        timestamp=timestamp_ms / 1000.0,
    )


def make_streaming_nav_event(url: str, timestamp: float) -> StreamingExfiltratedEvent:
    return StreamingExfiltratedEvent(
        event_name="nav:frame_started_navigating",
        params={"url": url},
        source=StreamingExfiltratedEventSource.CDP,
        timestamp=timestamp,
    )


def make_streaming_console_input(
    *,
    timestamp_ms: float,
    input_value: str,
    target_id: str = "email",
    target_text: str = "Email",
) -> list[StreamingExfiltratedEvent]:
    target = {
        "id": target_id,
        "skyId": "sky-email",
        "tagName": "INPUT",
        "text": [target_text],
        "value": input_value,
    }
    common = {
        "target": target,
        "timestamp": timestamp_ms,
        "url": "https://example.com",
        "activeElement": {"tagName": "INPUT"},
        "window": {"height": 800, "width": 1200, "scrollX": 0, "scrollY": 0},
        "mousePosition": {"xp": 0.5, "yp": 0.5},
    }

    return [
        StreamingExfiltratedEvent(
            event_name="user_interaction",
            params={"type": "focus", **common},
            source=StreamingExfiltratedEventSource.CONSOLE,
            timestamp=timestamp_ms / 1000.0,
        ),
        StreamingExfiltratedEvent(
            event_name="user_interaction",
            params={"type": "keydown", "key": "a", **common},
            source=StreamingExfiltratedEventSource.CONSOLE,
            timestamp=(timestamp_ms + 1) / 1000.0,
        ),
        StreamingExfiltratedEvent(
            event_name="user_interaction",
            params={"type": "blur", **common},
            source=StreamingExfiltratedEventSource.CONSOLE,
            timestamp=(timestamp_ms + 2) / 1000.0,
        ),
    ]


def test_create_url_block_is_deterministic() -> None:
    action = ActionUrlChange(
        kind=ActionKind.URL_CHANGE,
        target=_empty_action_target(),
        timestamp_start=1000.0,
        timestamp_end=1000.0,
        url="https://example.com/products?page=2",
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    block = asyncio.run(processor.create_url_block(action))

    assert block.label == "goto_example_com"
    assert block.url == "https://example.com/products?page=2"


def test_create_wait_block_is_deterministic() -> None:
    action = ActionWait(
        kind=ActionKind.WAIT,
        target=_empty_action_target(),
        timestamp_start=1000.0,
        timestamp_end=8000.0,
        url="https://example.com",
        duration_ms=7000,
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    block = asyncio.run(processor.create_wait_block(action))

    assert block.label == "wait_7s"
    assert block.wait_sec == 7


def test_create_wait_block_floors_at_minimum_duration() -> None:
    action = ActionWait(
        kind=ActionKind.WAIT,
        target=_empty_action_target(),
        timestamp_start=1000.0,
        timestamp_end=2000.0,
        url="https://example.com",
        duration_ms=1000,
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    block = asyncio.run(processor.create_wait_block(action))

    assert block.wait_sec == ActionWait.MIN_DURATION_THRESHOLD_MS // 1000


def test_input_text_parameter_key_is_derived_from_target_metadata() -> None:
    action = ActionInputText(
        kind=ActionKind.INPUT_TEXT,
        target=ActionTarget(
            id="customer_email",
            sky_id="sky-email",
            tag_name="INPUT",
            texts=["Email"],
            mouse=Mouse(xp=0.5, yp=0.5),
        ),
        timestamp_start=1000.0,
        timestamp_end=1001.0,
        url="https://example.com",
        input_value="secret123",
    )

    assert deterministic_input_text_parameter_key(action) == "customer_email"


@pytest.mark.asyncio
async def test_input_text_placeholder_parameterizes_value_on_enrichment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_llm(*args: t.Any, **kwargs: t.Any) -> dict[str, t.Any]:
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr(app, "LLM_API_HANDLER", failing_llm)

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda update: None,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
    )

    session.ingest_events(make_streaming_console_input(timestamp_ms=1000.0, input_value="secret123"))
    steps = await session.flush()

    assert len(steps) == 1
    assert steps[0].status == RecordingDraftStepStatus.READY
    assert "secret123" not in (steps[0].navigation_goal or "")
    assert steps[0].navigation_goal == "Fill 'Email' with {{ email }}."
    assert steps[0].parameter_keys == ["email"]
    assert steps[0].parameters == [{"key": "email"}]


@pytest.mark.asyncio
async def test_live_interpretation_emits_placeholder_then_enriched(monkeypatch: pytest.MonkeyPatch) -> None:
    release_llm = asyncio.Event()

    async def fake_llm(*args: t.Any, **kwargs: t.Any) -> dict[str, t.Any]:
        await release_llm.wait()
        return {
            "block_label": "click_submit",
            "title": "Click Submit",
            "prompt": "Click the submit button.",
        }

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

    session.ingest_events([make_streaming_console_click(timestamp_ms=1000.0)])
    await asyncio.sleep(0.05)

    # the placeholder draft is visible before the LLM responds
    updates_with_steps = [update for update in updates if update.steps]
    assert updates_with_steps
    placeholder = updates_with_steps[-1].steps[0]
    assert placeholder.status == RecordingDraftStepStatus.INTERPRETING
    assert placeholder.title == "Click 'Click me'"
    assert placeholder.navigation_goal == "Click 'Click me'."

    release_llm.set()
    steps = await session.flush()

    assert len(steps) == 1
    assert steps[0].status == RecordingDraftStepStatus.READY
    assert steps[0].title == "Click Submit"
    assert steps[0].navigation_goal == "Click the submit button."
    assert updates[-1].finalized is True


@pytest.mark.asyncio
async def test_live_interpretation_enrichment_failure_keeps_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_llm(*args: t.Any, **kwargs: t.Any) -> dict[str, t.Any]:
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr(app, "LLM_API_HANDLER", failing_llm)

    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda update: None,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
    )

    session.ingest_events([make_streaming_console_click(timestamp_ms=1000.0)])
    steps = await session.flush()

    assert len(steps) == 1
    assert steps[0].status == RecordingDraftStepStatus.READY
    assert steps[0].title == "Click 'Click me'"


@pytest.mark.asyncio
async def test_live_interpretation_nav_then_click_emits_two_steps() -> None:
    updates: list[RecordingInterpretationUpdate] = []
    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=updates.append,
        debounce_seconds=0.01,
        max_wait_seconds=0.05,
    )

    try:
        session.ingest_events(
            [make_streaming_nav_event(url="https://example.com/home", timestamp=1.0)],
        )
        await asyncio.sleep(0.05)

        click_event = make_streaming_console_click(timestamp_ms=2000.0)
        click_event.params = {**click_event.params, "url": "https://example.com/home"}
        session.ingest_events([click_event])

        steps = await session.flush()
        assert len(steps) == 2
        assert steps[0].block_type == "goto_url"
        assert steps[0].url == "https://example.com/home"
        assert steps[1].block_type == "action"
        assert steps[1].action_kind == ActionKind.CLICK
    finally:
        session.cancel()


@pytest.mark.asyncio
async def test_live_interpretation_max_wait_fires_during_continuous_events() -> None:
    updates: list[RecordingInterpretationUpdate] = []
    session = RecordingInterpretationSession(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=updates.append,
        debounce_seconds=0.05,
        max_wait_seconds=0.1,
    )

    # Significant events arrive faster than the quiet debounce, so a pure
    # trailing debounce would defer interpretation until the stream stops.
    start = time.monotonic()
    sequence = 0
    while time.monotonic() - start < 0.35:
        session.ingest_events([make_streaming_nav_event(url=f"https://example.com/{sequence}", timestamp=time.time())])
        sequence += 1
        await asyncio.sleep(0.02)

    try:
        assert any(update.steps for update in updates)
    finally:
        session.cancel()
        # let the cancelled debounce task unwind before the loop closes
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_network_activity_trailing_flush_preserves_throttled_activity() -> None:
    events: list[StreamingExfiltratedEvent] = []
    channel = ExfiltrationChannel(
        on_event=lambda messages: events.extend(messages),
        vnc_channel=t.cast(t.Any, DummyVncChannel()),
    )
    channel.NETWORK_ACTIVITY_THROTTLE_SECONDS = 0.01

    channel._handle_network_activity()
    assert len(events) == 1
    assert events[0].params == {"count": 1}

    channel._handle_network_activity()
    assert len(events) == 1

    await asyncio.sleep(0.02)

    assert len(events) == 2
    assert events[1].event_name == "net:activity"
    assert events[1].params == {"count": 1}


def make_cdp_event(
    event_name: str, timestamp_seconds: float, params: dict[str, t.Any] | None = None
) -> ExfiltratedCdpEvent:
    return ExfiltratedCdpEvent(
        kind="exfiltrated-event",
        event_name=event_name,
        params=ExfiltratedEventCdpParams(**(params or {})),
        source="cdp",
        timestamp=timestamp_seconds,
    )


def make_focus_event(target: dict[str, t.Any], timestamp: float) -> ExfiltratedConsoleEvent:
    params: dict[str, t.Any] = {
        "type": "focus",
        "target": target,
        "timestamp": timestamp,
    }

    return make_console_event(params=params, timestamp=timestamp)


def test_wait_suppressed_when_page_idle() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])

    events = [
        make_click_event(target=target, timestamp=1000.0),
        make_focus_event(target=target, timestamp=8000.0),
    ]

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions(events)

    assert [action.kind for action in actions] == [ActionKind.CLICK]


def test_wait_emitted_and_sized_to_page_busy_span() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])

    events = [
        make_click_event(target=target, timestamp=1000.0),
        # page busy until ~7s after the click (cdp timestamps are seconds)
        make_cdp_event("net:activity", timestamp_seconds=4.0, params={"count": 12}),
        make_cdp_event("net:activity", timestamp_seconds=7.0, params={"count": 3}),
        make_focus_event(target=target, timestamp=9000.0),
    ]

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions(events)

    assert [action.kind for action in actions] == [ActionKind.CLICK, ActionKind.WAIT]
    wait_action = actions[1]
    assert isinstance(wait_action, ActionWait)
    # busy span = last activity (7000) - click (1000); the 2s idle tail is excluded.
    assert wait_action.duration_ms == 6000


def test_wait_suppressed_when_page_settles_before_threshold() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])

    events = [
        make_click_event(target=target, timestamp=1000.0),
        # page settles ~1.5s in, then a long idle tail — below the wait threshold
        make_cdp_event("net:activity", timestamp_seconds=2.5, params={"count": 4}),
        make_focus_event(target=target, timestamp=12000.0),
    ]

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions(events)

    assert [action.kind for action in actions] == [ActionKind.CLICK]


def _two_consecutive_wait_events() -> list[t.Any]:
    # Two busy stretches (focus events produce no action) yield two adjacent waits;
    # a wait resets the timer, so each stretch needs its own pair of focus events.
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])
    return [
        make_focus_event(target=target, timestamp=1000.0),
        make_cdp_event("net:activity", timestamp_seconds=6.5, params={"count": 9}),
        make_focus_event(target=target, timestamp=7000.0),
        make_focus_event(target=target, timestamp=13000.0),
        make_cdp_event("net:activity", timestamp_seconds=18.5, params={"count": 9}),
        make_focus_event(target=target, timestamp=19000.0),
    ]


def test_events_to_actions_keeps_waits_separate_for_live_path() -> None:
    # events_to_actions feeds the incremental live interpreter, which tracks
    # actions by index, so it must stay append-only (no collapsing here).
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions(_two_consecutive_wait_events())

    assert [action.kind for action in actions] == [ActionKind.WAIT, ActionKind.WAIT]


def test_collapse_consecutive_waits_merges_durations() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions(_two_consecutive_wait_events())

    collapsed = Processor._collapse_consecutive_waits(actions)

    assert [action.kind for action in collapsed] == [ActionKind.WAIT]
    wait_action = collapsed[0]
    assert isinstance(wait_action, ActionWait)
    # 5500ms + 5500ms busy spans summed into a single wait.
    assert wait_action.duration_ms == 11000


def make_skewed_console_event(
    event_type: str,
    target: dict[str, t.Any],
    client_ms: float,
    server_skew_seconds: float,
) -> ExfiltratedConsoleEvent:
    """A console event whose server clock is offset from the client clock."""
    params: dict[str, t.Any] = {
        "type": event_type,
        "target": target,
        "timestamp": client_ms,
        "url": "https://example.com",
        "activeElement": {"tagName": "BUTTON"},
        "window": {"height": 800, "width": 1200, "scrollX": 0, "scrollY": 0},
        "mousePosition": {"xp": 0.5, "yp": 0.5},
    }
    return ExfiltratedConsoleEvent(
        kind="exfiltrated-event",
        source="console",
        event_name="user_interaction",
        params=params,
        timestamp=client_ms / 1000.0 + server_skew_seconds,
    )


def test_wait_offset_projection_cancels_client_server_clock_skew() -> None:
    # Server clock runs 60s ahead of the client clock. The Wait machine must
    # project the server-stamped CDP activity back into the client clock so the
    # busy span is measured correctly; otherwise the activity falls outside the
    # client-clock gap and the (real) wait is wrongly suppressed.
    skew = 60.0
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])

    events = [
        make_skewed_console_event("click", target, client_ms=1000.0, server_skew_seconds=skew),
        # real activity at client 4s/7s -> server-stamped 64s/67s
        make_cdp_event("net:activity", timestamp_seconds=4.0 + skew, params={"count": 12}),
        make_cdp_event("net:activity", timestamp_seconds=7.0 + skew, params={"count": 3}),
        make_skewed_console_event("focus", target, client_ms=9000.0, server_skew_seconds=skew),
    ]

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions(events)

    assert [action.kind for action in actions] == [ActionKind.CLICK, ActionKind.WAIT]
    wait_action = actions[1]
    assert isinstance(wait_action, ActionWait)
    assert wait_action.duration_ms == 6000


def test_wait_ignores_activity_outside_the_idle_gap() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])

    events = [
        # activity happened before the gap even started
        make_cdp_event("net:activity", timestamp_seconds=0.5, params={"count": 3}),
        make_click_event(target=target, timestamp=1000.0),
        make_focus_event(target=target, timestamp=8000.0),
    ]

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions(events)

    assert [action.kind for action in actions] == [ActionKind.CLICK]


def test_summarize_exfiltrated_recording_events_mixed() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])
    click = make_click_event(target=target, timestamp=1000.0)
    keypress = make_console_event(
        params={
            "type": "keypress",
            "target": target,
            "timestamp": 1001.0,
        },
        timestamp=1001.0,
    )
    cdp_nav = ExfiltratedCdpEvent(
        kind="exfiltrated-event",
        event_name="nav:frame_navigated",
        params=ExfiltratedEventCdpParams(),
        source="cdp",
        timestamp=999.0,
    )
    cdp_nav_2 = ExfiltratedCdpEvent(
        kind="exfiltrated-event",
        event_name="nav:frame_navigated",
        params=ExfiltratedEventCdpParams(),
        source="cdp",
        timestamp=1002.0,
    )

    summary = summarize_exfiltrated_recording_events([cdp_nav, click, keypress, cdp_nav_2])

    assert summary["recording_exfil_total_events"] == 4
    assert summary["recording_exfil_cdp_event_count"] == 2
    assert summary["recording_exfil_console_event_count"] == 2
    assert summary["recording_exfil_cdp_event_name_counts"] == {"nav:frame_navigated": 2}
    assert summary["recording_exfil_console_dom_type_counts"] == {"click": 1, "keypress": 1}
    assert summary["recording_exfil_console_exfil_event_name_counts"] == {"user_interaction": 2}
