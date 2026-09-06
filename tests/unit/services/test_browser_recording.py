"""
Just an example unit test for now. Will expand later.
"""

import asyncio
import base64
import gzip
import re
import time
import typing as t
import zlib
from pathlib import Path

import pytest

import skyvern
from skyvern.forge import app
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEvent as StreamingExfiltratedEvent
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import (
    ExfiltratedEventSource as StreamingExfiltratedEventSource,
)
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import (
    ExfiltrationChannel,
)
from skyvern.services.browser_recording import redact as redact_module
from skyvern.services.browser_recording.interpretation import RecordingInterpretationSession
from skyvern.services.browser_recording.service import (
    DUPLICATE_ACTION_WINDOW_MS,
    Processor,
    _gunzip_bounded,
    _is_duplicate_action,
    _recording_enrichment_llm_handler,
    _resolve_enrichment_handler,
    deterministic_input_text_parameter_key,
    summarize_exfiltrated_recording_events,
)
from skyvern.services.browser_recording.types import (
    ActionClick,
    ActionInputText,
    ActionKind,
    ActionTarget,
    ActionUrlChange,
    ActionWait,
    ExfiltratedCdpEvent,
    ExfiltratedConsoleEvent,
    ExfiltratedEventCdpParams,
    Mouse,
    RecordingDraftStep,
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


def _click_action(*, timestamp: float, sky_id: str = "sky-123", target_id: str = "button-1") -> ActionClick:
    return ActionClick(
        kind=ActionKind.CLICK,
        target=ActionTarget(
            id=target_id,
            sky_id=sky_id,
            tag_name="BUTTON",
            texts=["Click me"],
            mouse=Mouse(xp=0.5, yp=0.5),
        ),
        timestamp_start=timestamp,
        timestamp_end=timestamp,
        url="https://example.com",
    )


def test_is_duplicate_action_empty_list_is_not_duplicate() -> None:
    assert _is_duplicate_action(_click_action(timestamp=1000.0), []) is False


def test_is_duplicate_action_suppresses_jittered_recapture() -> None:
    existing = [_click_action(timestamp=1000.0)]
    jittered = _click_action(timestamp=1000.0 + 2)

    assert _is_duplicate_action(jittered, existing) is True


def test_is_duplicate_action_suppresses_non_adjacent_duplicate() -> None:
    existing = [
        _click_action(timestamp=1000.0, sky_id="sky-a", target_id="a"),
        _click_action(timestamp=1010.0, sky_id="sky-b", target_id="b"),
    ]
    duplicate_of_first = _click_action(timestamp=1005.0, sky_id="sky-a", target_id="a")

    assert _is_duplicate_action(duplicate_of_first, existing) is True


def test_is_duplicate_action_keeps_intentional_repeat_outside_window() -> None:
    existing = [_click_action(timestamp=1000.0)]
    later_repeat = _click_action(timestamp=1000.0 + DUPLICATE_ACTION_WINDOW_MS + 1)

    assert _is_duplicate_action(later_repeat, existing) is False


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


def make_streaming_console_click(
    timestamp_ms: float,
    *,
    target_id: str = "button-1",
    target_text: str = "Click me",
    accessible_name: str | None = None,
) -> StreamingExfiltratedEvent:
    target: dict[str, t.Any] = {
        "id": target_id,
        "skyId": "sky-123",
        "tagName": "BUTTON",
        "text": [target_text],
    }
    if accessible_name is not None:
        target["accessibleName"] = accessible_name
    return StreamingExfiltratedEvent(
        event_name="user_interaction",
        params={
            "type": "click",
            "target": target,
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
    input_type: str | None = None,
    autocomplete: str | None = None,
    accessible_name: str | None = None,
) -> list[StreamingExfiltratedEvent]:
    target: dict[str, t.Any] = {
        "id": target_id,
        "skyId": "sky-email",
        "tagName": "INPUT",
        "text": [target_text],
        "value": input_value,
    }
    if input_type is not None:
        target["inputType"] = input_type
    if autocomplete is not None:
        target["autocomplete"] = autocomplete
    if accessible_name is not None:
        target["accessibleName"] = accessible_name
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


def test_enrichment_handler_uses_dedicated_key_when_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.services.browser_recording.service as svc

    _resolve_enrichment_handler.cache_clear()
    dedicated = object()
    monkeypatch.setattr(svc.settings, "RECORDING_ENRICHMENT_LLM_KEY", "SOME_KEY")
    monkeypatch.setattr(svc.LLMConfigRegistry, "is_registered", lambda key: True)
    monkeypatch.setattr(svc.LLMAPIHandlerFactory, "get_llm_api_handler", lambda key: dedicated)

    assert _recording_enrichment_llm_handler() is dedicated


def test_enrichment_handler_falls_back_when_key_unregistered(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.services.browser_recording.service as svc

    _resolve_enrichment_handler.cache_clear()
    default = object()
    monkeypatch.setattr(svc.settings, "RECORDING_ENRICHMENT_LLM_KEY", "SOME_KEY")
    monkeypatch.setattr(svc.LLMConfigRegistry, "is_registered", lambda key: False)
    monkeypatch.setattr(app, "LLM_API_HANDLER", default)

    assert _recording_enrichment_llm_handler() is default


def test_enrichment_handler_falls_back_on_resolution_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.services.browser_recording.service as svc

    _resolve_enrichment_handler.cache_clear()
    default = object()

    def boom(key: str) -> bool:
        raise RuntimeError("registry blew up")

    monkeypatch.setattr(svc.settings, "RECORDING_ENRICHMENT_LLM_KEY", "SOME_KEY")
    monkeypatch.setattr(svc.LLMConfigRegistry, "is_registered", boom)
    monkeypatch.setattr(app, "LLM_API_HANDLER", default)

    assert _recording_enrichment_llm_handler() is default


def test_enrichment_handler_memoizes_dedicated_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.services.browser_recording.service as svc

    _resolve_enrichment_handler.cache_clear()
    dedicated = object()
    calls = {"is_registered": 0}

    def counting_is_registered(key: str) -> bool:
        calls["is_registered"] += 1
        return True

    monkeypatch.setattr(svc.settings, "RECORDING_ENRICHMENT_LLM_KEY", "SOME_KEY")
    monkeypatch.setattr(svc.LLMConfigRegistry, "is_registered", counting_is_registered)
    monkeypatch.setattr(svc.LLMAPIHandlerFactory, "get_llm_api_handler", lambda key: dedicated)

    assert _recording_enrichment_llm_handler() is dedicated
    assert _recording_enrichment_llm_handler() is dedicated
    assert calls["is_registered"] == 1


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

    # the placeholder draft is visible before the LLM responds — it arrives as a
    # delta (changed_steps), since only snapshots carry the full steps list.
    def emitted_steps(u: RecordingInterpretationUpdate) -> list[RecordingDraftStep]:
        return u.steps if u.is_snapshot else u.changed_steps

    updates_with_steps = [update for update in updates if emitted_steps(update)]
    assert updates_with_steps
    placeholder = emitted_steps(updates_with_steps[-1])[0]
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
async def test_live_interpretation_nav_then_click_emits_two_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real coroutine function, not the ambient AsyncMock: cancel() below drops the in-flight
    # enrichment task, and an orphaned AsyncMock coroutine surfaces at GC time as an asyncio
    # error event inside whatever unrelated test happens to be running then.
    async def stub_llm(*args: t.Any, **kwargs: t.Any) -> dict[str, t.Any]:
        return {"block_label": "act", "title": "Browser Action", "prompt": ""}

    monkeypatch.setattr(app, "LLM_API_HANDLER", stub_llm)

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
        # steps land in changed_steps (delta) or steps (snapshot) depending on emit type.
        assert any(update.steps or update.changed_steps for update in updates)
    finally:
        session.cancel()
        # let the cancelled debounce task unwind before the loop closes
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_network_activity_trailing_flush_preserves_throttled_activity() -> None:
    events: list[StreamingExfiltratedEvent] = []
    channel = ExfiltrationChannel(
        on_event=lambda messages: events.extend(messages),
        context=t.cast(t.Any, DummyVncChannel()),
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


def test_gunzip_bounded_returns_full_payload_under_limit() -> None:
    payload = b"hello world" * 500
    compressed = gzip.compress(payload)

    assert _gunzip_bounded(compressed, len(payload)) == payload


def test_gunzip_bounded_allows_output_exactly_at_limit() -> None:
    payload = b"A" * 4096
    compressed = gzip.compress(payload)

    assert _gunzip_bounded(compressed, len(payload)) == payload


def test_gunzip_bounded_rejects_output_over_limit() -> None:
    payload = b"A" * 4096
    compressed = gzip.compress(payload)

    assert _gunzip_bounded(compressed, len(payload) - 1) is None


def test_gunzip_bounded_rejects_decompression_bomb() -> None:
    # ~32 MiB of zeros compresses to a few KiB; the bound must reject it long before
    # the full output would be materialized in memory.
    bomb = gzip.compress(b"\x00" * (32 * 1024 * 1024))
    assert len(bomb) < 1 * 1024 * 1024

    assert _gunzip_bounded(bomb, 1024) is None


def test_gunzip_bounded_raises_on_corrupt_input() -> None:
    with pytest.raises(zlib.error):
        _gunzip_bounded(b"not a valid gzip stream", 1024)


def test_decompress_rejects_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.services.browser_recording.service as svc

    monkeypatch.setattr(svc, "MAX_DECOMPRESSED_SIZE", 1024)
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    bomb = base64.b64encode(gzip.compress(b"\x00" * (10 * 1024 * 1024))).decode("ascii")

    assert processor.decompress(bomb) is None


def test_decompress_returns_bytes_for_valid_payload() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    raw = b'[{"source": "cdp", "event_name": "nav:frame_navigated"}]'
    payload = base64.b64encode(gzip.compress(raw)).decode("ascii")

    assert processor.decompress(payload) == raw


def make_keydown_event(target: dict[str, t.Any], timestamp: float, key: str = "a") -> ExfiltratedConsoleEvent:
    return make_console_event(
        params={"type": "keydown", "key": key, "target": target, "timestamp": timestamp},
        timestamp=timestamp,
    )


def make_blur_event(target: dict[str, t.Any], timestamp: float) -> ExfiltratedConsoleEvent:
    return make_console_event(
        params={"type": "blur", "target": target, "timestamp": timestamp},
        timestamp=timestamp,
    )


def test_is_secret_field_and_credential_kind() -> None:
    from skyvern.services.browser_recording.redact import credential_kind_for_target, is_secret_field

    assert is_secret_field("password", None) is True
    assert is_secret_field("text", "one-time-code") is True
    assert is_secret_field("text", "cc-number") is True
    assert is_secret_field("text", "username current-password") is True
    assert is_secret_field("email", None) is False
    assert is_secret_field("text", "off") is False
    assert is_secret_field("text", "username") is False

    assert credential_kind_for_target("password", None) == "password"
    assert credential_kind_for_target("text", "current-password") == "password"
    assert credential_kind_for_target("text", "one-time-code") == "totp"
    assert credential_kind_for_target("text", "cc-number") == "credit_card"
    assert credential_kind_for_target("text", "cc-name") == "credit_card"
    assert credential_kind_for_target("email", None) is None
    assert credential_kind_for_target("text", "off") is None

    assert credential_kind_for_target("text", None, field_id="otp", texts=["Verification code"]) == "totp"
    assert credential_kind_for_target("password", None, accessible_name="API Key") == "secret"
    assert credential_kind_for_target("text", None, texts=["Card number"], tag_name="input") == "credit_card"

    # `texts` carries innerText for elements with children, so it is only a label on <input>.
    # A <select> whose options happen to contain a hint phrase must keep its recorded value.
    assert credential_kind_for_target("text", None, texts=["Card number"], tag_name="select") is None
    assert (
        credential_kind_for_target("text", None, texts=["Reason: Verification code issue"], tag_name="select") is None
    )
    assert credential_kind_for_target(None, None, texts=["Two factor authentication"], tag_name="div") is None
    # The label itself still classifies, whatever the tag.
    assert credential_kind_for_target("text", None, accessible_name="Verification code", tag_name="select") == "totp"
    assert credential_kind_for_target("password", None, accessible_name="Password") == "password"
    assert credential_kind_for_target(None, None, accessible_name="Email me a magic link") == "magic_link"
    assert is_secret_field("password", None, accessible_name="API Key") is True
    assert is_secret_field(None, None, accessible_name="Email me a magic link") is False


def test_reify_strips_secret_values_from_console_events() -> None:
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    events = processor.reify(
        [
            {
                "kind": "exfiltrated-event",
                "event_name": "user_interaction",
                "source": "console",
                "timestamp": 1.0,
                "params": {
                    "type": "keydown",
                    "key": "h",
                    "code": "KeyH",
                    "inputValue": "hunter2",
                    "url": "https://example.com/login",
                    "timestamp": 1000.0,
                    "target": {
                        "tagName": "INPUT",
                        "id": "password",
                        "skyId": "sky-pw",
                        "inputType": "password",
                        "value": "hunter2",
                        "text": ["Password"],
                    },
                    "activeElement": {"tagName": "INPUT"},
                    "window": {"height": 800, "width": 1200, "scrollX": 0, "scrollY": 0},
                    "mousePosition": {"xp": 0.5, "yp": 0.5},
                },
            }
        ]
    )

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ExfiltratedConsoleEvent)
    assert event.params.target.value is None
    assert event.params.inputValue is None
    assert event.params.key is None
    assert event.params.code is None
    assert event.params.target.inputType == "password"


def test_password_fill_still_emits_empty_input_text() -> None:
    target = {
        "id": "password",
        "skyId": "sky-pw",
        "tagName": "INPUT",
        "text": ["Password"],
        "inputType": "password",
        "value": None,
    }
    events = [
        make_focus_event(target=target, timestamp=1000.0),
        make_keydown_event(target=target, timestamp=1001.0),
        make_blur_event(target=target, timestamp=1002.0),
    ]

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions(events)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, ActionInputText)
    assert action.input_value == ""
    assert action.target.input_type == "password"


def _js_string_literals(source: str, name: str) -> set[str]:
    match = re.search(rf"const {name} = (?:new Set\()?\[(.*?)\]", source, re.DOTALL)
    assert match, f"{name} not found in exfiltrate.js"
    return set(re.findall(r'"([^"]*)"', match.group(1)))


def test_secret_matcher_lists_match_between_js_and_python() -> None:
    """The page script and the Python ingest must classify the same fields as secret.

    A token added to one side only silently changes what gets captured, so parity is
    asserted here rather than left to the docstring in redact.py.
    """
    js_source = (Path(skyvern.__file__).parent / "forge/sdk/routes/streaming/channels/js/exfiltrate.js").read_text()
    matcher = re.search(r"const isSecretField = \(element\) => \{(.*?)\n    \};", js_source, re.DOTALL)
    assert matcher, "isSecretField not found in exfiltrate.js"
    js_matcher_body = matcher.group(1)

    # MAGIC_LINK_HINT_PHRASES is deliberately absent from the JS: "magic_link" is not a
    # secret kind, so it labels a field without redacting it.
    for name, python_value in (
        ("SECRET_INPUT_TYPES", redact_module.SECRET_INPUT_TYPES),
        ("SECRET_AUTOCOMPLETE_TOKENS", redact_module.SECRET_AUTOCOMPLETE_TOKENS),
        ("CREDIT_CARD_HINT_PHRASES", redact_module.CREDIT_CARD_HINT_PHRASES),
        ("TOTP_HINT_PHRASES", redact_module.TOTP_HINT_PHRASES),
        ("SECRET_HINT_PHRASES", redact_module.SECRET_HINT_PHRASES),
    ):
        assert _js_string_literals(js_source, name) == set(python_value), (
            f"{name} differs between exfiltrate.js and redact.py"
        )
        # Matching lists are worthless if the page script stops consulting one: deleting a
        # clause from isSecretField while leaving its array declared would keep the parity
        # assertion above green and silently stop redacting that family at source.
        assert name in js_matcher_body, f"{name} is declared but never used by isSecretField"


def test_password_submitted_with_enter_still_emits_input_text() -> None:
    """Enter-to-submit never blurs before navigating, so the Enter keydown is the only emit signal.

    Redacting it along with the character keystrokes silently drops the whole password step.
    """
    target = {
        "id": "password",
        "skyId": "sky-pw",
        "tagName": "INPUT",
        "text": ["Password"],
        "inputType": "password",
        "value": None,
    }
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    # Enter through reify(), not events_to_actions(): ingest redaction runs there, so a test
    # below it would pass even with the Enter keydown stripped.
    events = processor.reify(
        [
            make_focus_event(target=target, timestamp=1000.0).model_dump(),
            make_keydown_event(target=target, timestamp=1001.0, key="s").model_dump(),
            make_keydown_event(target=target, timestamp=1002.0, key="Enter").model_dump(),
        ]
    )
    actions = processor.events_to_actions(events)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, ActionInputText)
    assert action.input_value == ""
    assert action.target.input_type == "password"


def test_secret_character_keystrokes_still_redacted() -> None:
    from skyvern.services.browser_recording.redact import redact_console_event

    target = {"id": "password", "tagName": "INPUT", "inputType": "password", "value": "hunter2"}

    typed = redact_console_event(make_keydown_event(target=target, timestamp=1.0, key="s"))
    assert typed.params.key is None
    assert typed.params.code is None
    assert typed.params.target.value is None

    submit = redact_console_event(make_keydown_event(target=target, timestamp=2.0, key="Enter"))
    assert submit.params.key == "Enter"
    assert submit.params.target.value is None


def test_phrase_only_secret_has_its_value_stripped_through_the_processor() -> None:
    """A field classified *only* by its label must lose its value, not just get a kind.

    The password/OTP cases are carried by input_type/autocomplete, so neither proves the
    phrase matching this PR adds actually reaches redaction.
    """
    target = {
        "id": "field-1",
        "tagName": "INPUT",
        "inputType": "text",
        "autocomplete": None,
        "accessibleName": "API Key",
        "value": "sk_live_not_real",
    }
    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    events = processor.reify(
        [
            make_focus_event(target=target, timestamp=1000.0).model_dump(),
            make_keydown_event(target=target, timestamp=1001.0, key="s").model_dump(),
            make_blur_event(target=target, timestamp=1002.0).model_dump(),
        ]
    )
    actions = processor.events_to_actions(events)

    assert len(actions) == 1
    action = actions[0]
    assert isinstance(action, ActionInputText)
    assert action.input_value == ""
    assert "sk_live_not_real" not in repr(action)


def test_non_secret_empty_value_still_skipped() -> None:
    target = {
        "id": "email",
        "skyId": "sky-email",
        "tagName": "INPUT",
        "text": ["Email"],
        "inputType": "email",
        "value": None,
    }
    events = [
        make_focus_event(target=target, timestamp=1000.0),
        make_keydown_event(target=target, timestamp=1001.0),
        make_blur_event(target=target, timestamp=1002.0),
    ]

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    assert processor.events_to_actions(events) == []


@pytest.mark.asyncio
async def test_password_ingest_emits_redacted_draft_with_credential_kind(
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

    session.ingest_events(
        make_streaming_console_input(
            timestamp_ms=1000.0,
            input_value="hunter2",
            target_id="password",
            target_text="Password",
            input_type="password",
        )
    )
    steps = await session.flush()

    assert len(steps) == 1
    assert steps[0].credential_kind == "password"
    assert "hunter2" not in (steps[0].title or "")
    assert "hunter2" not in (steps[0].navigation_goal or "")
    stored = session.events
    for event in stored:
        if isinstance(event, ExfiltratedConsoleEvent):
            assert event.params.target.value != "hunter2"
            assert event.params.inputValue != "hunter2"


@pytest.mark.asyncio
async def test_create_action_block_prompt_omits_secret_keeps_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import skyvern.services.browser_recording.service as svc

    captured: dict[str, str] = {}

    async def fake_llm(*, prompt: str, prompt_name: str, organization_id: str) -> dict[str, t.Any]:
        captured[prompt_name] = prompt
        return {"block_label": "fill", "title": "Fill", "prompt": "Fill the field."}

    monkeypatch.setattr(svc, "_recording_enrichment_llm_handler", lambda: fake_llm)

    processor = Processor(PBS_ID, ORG_ID, WP_ID)

    await processor.create_action_block(
        ActionInputText(
            kind=ActionKind.INPUT_TEXT,
            target=ActionTarget(
                id="password",
                sky_id="sky-pw",
                tag_name="INPUT",
                texts=["Password"],
                input_type="password",
                mouse=Mouse(xp=0.5, yp=0.5),
            ),
            timestamp_start=1000.0,
            timestamp_end=1001.0,
            url="https://example.com/login",
            input_value="hunter2",
        )
    )
    assert "hunter2" not in captured["recording-action-block-prompt-input-text"]

    captured.clear()
    await processor.create_action_block(
        ActionInputText(
            kind=ActionKind.INPUT_TEXT,
            target=ActionTarget(
                id="email",
                sky_id="sky-email",
                tag_name="INPUT",
                texts=["Email"],
                input_type="email",
                mouse=Mouse(xp=0.5, yp=0.5),
            ),
            timestamp_start=1000.0,
            timestamp_end=1001.0,
            url="https://example.com/login",
            input_value="user@example.com",
        )
    )
    assert "user@example.com" in captured["recording-action-block-prompt-input-text"]


@pytest.mark.asyncio
async def test_otp_autocomplete_ingest_redacts_and_stamps_totp(
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
    session.ingest_events(
        make_streaming_console_input(
            timestamp_ms=1000.0,
            input_value="654321",
            target_id="otp",
            target_text="Code",
            input_type="text",
            autocomplete="one-time-code",
        )
    )
    steps = await session.flush()

    assert len(steps) == 1
    assert steps[0].credential_kind == "totp"
    assert "654321" not in (steps[0].navigation_goal or "")


@pytest.mark.asyncio
async def test_api_key_ingest_stamps_secret_kind(
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
    session.ingest_events(
        make_streaming_console_input(
            timestamp_ms=1000.0,
            input_value="sk_live_secret",
            target_id="api-key",
            target_text="API Key",
            input_type="password",
            accessible_name="API Key",
        )
    )
    steps = await session.flush()

    assert len(steps) == 1
    assert steps[0].credential_kind == "secret"
    assert "sk_live_secret" not in (steps[0].navigation_goal or "")


@pytest.mark.asyncio
async def test_magic_link_click_stamps_kind(
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
    session.ingest_events(
        [
            make_streaming_console_click(
                1000.0,
                target_text="Email me a magic link",
                accessible_name="Email me a magic link",
            )
        ]
    )
    steps = await session.flush()

    assert len(steps) == 1
    assert steps[0].credential_kind == "magic_link"


def make_change_event(target: dict[str, t.Any], timestamp: float) -> ExfiltratedConsoleEvent:
    return make_console_event(
        params={"type": "change", "target": target, "timestamp": timestamp},
        timestamp=timestamp,
    )


SELECT_TARGET = {
    "id": "country",
    "skyId": "sky-select",
    "tagName": "SELECT",
    "text": ["Germany", "France"],
    "selector": "#country",
    "value": "DE",
}


def test_select_chosen_with_the_mouse_records_its_value() -> None:
    """A native select driven by mouse never reaches the input-text machine.

    That machine needs a keydown to leave its focus state, so before this the chosen option
    was dropped and the recording kept only the click that opened the dropdown.
    """
    events = [
        make_click_event(target=SELECT_TARGET, timestamp=1000.0),
        make_focus_event(target=SELECT_TARGET, timestamp=1010.0),
        make_change_event(target=SELECT_TARGET, timestamp=1100.0),
        make_blur_event(target=SELECT_TARGET, timestamp=1200.0),
    ]

    actions = Processor(PBS_ID, ORG_ID, WP_ID).events_to_actions(events)

    assert [a.kind for a in actions] == [ActionKind.CLICK, ActionKind.INPUT_TEXT]
    assert actions[1].input_value == "DE"
    assert actions[1].target.tag_name == "SELECT"


def test_select_chosen_with_the_keyboard_records_once() -> None:
    """The keyboard path reached the input-text machine already; it must not now double-emit."""
    events = [
        make_focus_event(target=SELECT_TARGET, timestamp=1000.0),
        make_keydown_event(target=SELECT_TARGET, timestamp=1100.0, key="ArrowDown"),
        make_change_event(target=SELECT_TARGET, timestamp=1150.0),
        make_blur_event(target=SELECT_TARGET, timestamp=1900.0),
    ]

    actions = Processor(PBS_ID, ORG_ID, WP_ID).events_to_actions(events)

    assert [a.kind for a in actions] == [ActionKind.INPUT_TEXT]
    assert actions[0].input_value == "DE"


def test_change_on_a_checkbox_records_only_the_click() -> None:
    """change fires for checkboxes and radios too, where the click already carries the gesture.

    Without the SELECT-only guard a ticked checkbox also mints a bogus fill step with the
    value "on".
    """
    target = {
        "id": "terms",
        "skyId": "sky-terms",
        "tagName": "INPUT",
        "inputType": "checkbox",
        "text": ["Accept terms"],
        "value": "on",
    }

    events = [
        make_click_event(target=target, timestamp=1000.0),
        make_change_event(target=target, timestamp=1010.0),
    ]

    actions = Processor(PBS_ID, ORG_ID, WP_ID).events_to_actions(events)

    assert [a.kind for a in actions] == [ActionKind.CLICK]


def test_select_placeholder_row_records_nothing() -> None:
    target = {**SELECT_TARGET, "value": ""}

    actions = Processor(PBS_ID, ORG_ID, WP_ID).events_to_actions([make_change_event(target=target, timestamp=1100.0)])

    assert actions == []


def test_secret_select_keeps_its_step_with_a_blank_value() -> None:
    """A card-expiry month is usually a <select autocomplete="cc-exp-month">.

    Ingest redaction nulls its value, so returning None on a missing value would drop the whole
    step and leave the user nothing to bind. StateMachineInputText keeps the step for the same
    reason; this asserts the select machine matches.
    """
    target = {
        "id": "cc-exp-month",
        "skyId": "sky-cc",
        "tagName": "SELECT",
        "text": ["01", "02"],
        "autocomplete": "cc-exp-month",
        "value": "03",
    }

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    # Through reify(), so ingest redaction runs exactly as it does in production.
    events = processor.reify(
        [
            make_click_event(target=target, timestamp=1000.0).model_dump(),
            make_change_event(target=target, timestamp=1100.0).model_dump(),
        ]
    )
    actions = processor.events_to_actions(events)

    assert [a.kind for a in actions] == [ActionKind.CLICK, ActionKind.INPUT_TEXT]
    assert actions[1].input_value == ""


def test_select_parameter_key_ignores_option_labels() -> None:
    """A select's texts are its option labels, not a label for the field.

    Without the guard a select with no id is named after whichever option is listed first, so
    the draft reads "Enter {{ germany }} into the country field".
    """
    target = {"skyId": "sky-country", "tagName": "SELECT", "text": ["Germany", "France"], "value": "DE"}

    actions = Processor(PBS_ID, ORG_ID, WP_ID).events_to_actions([make_change_event(target=target, timestamp=1000.0)])

    assert deterministic_input_text_parameter_key(actions[0]) == "sky_country"


def test_select_draft_step_is_named_after_the_field_not_an_option() -> None:
    """A select's texts are its option labels, so a country dropdown would read "Fill 'Germany'".

    The placeholder is what the user sees while enrichment runs, and what survives if it fails.
    """
    from skyvern.services.browser_recording.interpretation import _placeholder_step_from_action

    target = {"id": "country", "skyId": "sky-s", "tagName": "SELECT", "text": ["Germany", "France"], "value": "DE"}

    actions = Processor(PBS_ID, ORG_ID, WP_ID).events_to_actions([make_change_event(target=target, timestamp=1000.0)])
    step = _placeholder_step_from_action(browser_session_id=PBS_ID, action_index=0, action=actions[0])

    assert step.title == "Fill 'country'"
    assert "Germany" not in (step.label or "")


def test_select_type_ahead_records_the_option_the_user_landed_on() -> None:
    """Keyboard type-ahead fires change per keystroke as the selection walks the option list.

    Duplicate suppression keys on the target, so without the value in that key the second
    change is swallowed and the recording keeps an option the user only passed through.
    """
    target = {"id": "country", "skyId": "sky-c", "tagName": "SELECT", "text": ["Country"]}

    events = [
        make_focus_event(target=target, timestamp=1000.0),
        make_keydown_event(target=target, timestamp=1050.0, key="G"),
        make_change_event(target={**target, "value": "GE"}, timestamp=1060.0),
        make_keydown_event(target=target, timestamp=1100.0, key="e"),
        make_change_event(target={**target, "value": "DE"}, timestamp=1110.0),
        make_blur_event(target={**target, "value": "DE"}, timestamp=1400.0),
    ]

    actions = Processor(PBS_ID, ORG_ID, WP_ID).events_to_actions(events)

    assert actions[-1].input_value == "DE"


def test_labelled_select_keeps_its_field_label() -> None:
    """A labelled select's texts lead with the field label, not an option.

    getElementText pushes aria-label / aria-labelledby / placeholder / title ahead of option
    text, and accessible_name carries the same label, so neither should fall back to the tag.
    """
    from skyvern.services.browser_recording.interpretation import _placeholder_step_from_action

    target = {
        "skyId": "sky-c",
        "tagName": "SELECT",
        "text": ["Country", "Germany"],
        "accessibleName": "Country",
        "value": "DE",
    }

    actions = Processor(PBS_ID, ORG_ID, WP_ID).events_to_actions([make_change_event(target=target, timestamp=1000.0)])
    step = _placeholder_step_from_action(browser_session_id=PBS_ID, action_index=0, action=actions[0])

    assert step.title == "Fill 'Country'"
    assert deterministic_input_text_parameter_key(actions[0]) == "country"
