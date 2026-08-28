"""Message-channel resilience when the recorded browser target goes away (SKY-12366).

Production showed two crash shapes with the same engine: a recording message handler
touches a browser whose target is gone, the exception escapes ``handle_data``, and the
whole websocket loop dies. The frontend auto-reconnects and re-sends the recording
messages, producing a crash-loop with zero capture:

- ``BEGIN_EXFILTRATION`` -> ``ExfiltrationChannel.start()`` -> ``connect_over_cdp`` ->
  ``ECONNREFUSED`` / 502 (customer sessions, ~0.3s crash-loop, no drafts at all).
- ``END_EXFILTRATION`` -> ``ExfiltrationChannel.stop()`` -> ``TargetClosedError``
  (internal repro sessions), which also skipped the interpretation-session flush.

These tests drive the real ``loop_stream_messages`` with a scripted websocket and
assert the loop survives handler failures, surfaces them as ``MessageOutError``, and
never skips the draft flush or the channel close.
"""

from __future__ import annotations

import asyncio
import json
import time
import typing as t
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect
from playwright._impl._errors import Error as PlaywrightError
from starlette.websockets import WebSocketState

from skyvern.forge.sdk.routes.streaming.channels import message as message_module
from skyvern.forge.sdk.routes.streaming.channels.message import (
    MessageKind,
    MessageOutError,
    MessageOutRecordingCommitted,
    loop_stream_messages,
)
from skyvern.services.browser_recording.types import ActionKind
from skyvern.services.browser_recording.v2.ledger import Gesture, get_ledger, stop_ledger
from skyvern.services.browser_recording.v2.session import discard_session_v2, get_session_v2

PBS_ID = "pbs_123"
WP_ID = "wpid_123"

BEGIN_EXFILTRATION_DATA = {
    "kind": MessageKind.BEGIN_EXFILTRATION.value,
    "workflow_permanent_id": WP_ID,
    "live_interpretation_enabled": True,
    "recording_attempt_id": "attempt-1",
}
END_EXFILTRATION_DATA = {"kind": MessageKind.END_EXFILTRATION.value}
RECORDING_REARM_CAPTURE_DATA = {"kind": MessageKind.RECORDING_REARM_CAPTURE.value}


def _message_channel(receive_sequence: t.Iterable[object]) -> MagicMock:
    """A MessageChannel double whose websocket replays ``receive_sequence``."""
    message_channel = MagicMock()
    message_channel.class_name = "MessageChannel"
    message_channel.identity = {}
    message_channel.is_open = True
    message_channel.client_id = "client-1"
    message_channel.organization_id = "org_123"
    message_channel.browser_session = MagicMock(persistent_browser_session_id=PBS_ID)
    message_channel.websocket.receive_json = AsyncMock(side_effect=receive_sequence)
    message_channel.websocket.client_state = WebSocketState.CONNECTED
    # backend_to_frontend blocks on a real (empty) queue until cancelled at teardown.
    message_channel.out_queue = asyncio.Queue()
    message_channel.send_nowait = MagicMock()
    message_channel.close = AsyncMock()
    return message_channel


def _sent_error_kinds(message_channel: MagicMock) -> list[str]:
    kinds: list[str] = []
    for call in message_channel.send_nowait.call_args_list:
        for sent in call.kwargs.get("messages", []):
            if isinstance(sent, MessageOutError):
                kinds.append(sent.failed_kind)
    return kinds


def _install_recording_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    channel: object,
) -> MagicMock:
    """Stub the vnc registry, exfiltration channel factory, and interpretation registry."""
    context = MagicMock()
    context.organization_id = "org_123"
    context.x_api_key = "api-key-123"
    context.browser_session = MagicMock(persistent_browser_session_id=PBS_ID)
    context.identity = {"organization_id": "org_123", "browser_session_id": PBS_ID}
    monkeypatch.setattr(message_module, "get_vnc_channel", lambda _client_id: context)
    monkeypatch.setattr(message_module, "ExfiltrationChannel", MagicMock(return_value=channel))

    registry = MagicMock()
    registry.start_session = MagicMock()
    registry.ingest_events = MagicMock()
    registry.stop_session = AsyncMock(return_value=[])
    monkeypatch.setattr(message_module, "interpretation_registry", registry)
    return registry


@pytest.mark.asyncio
async def test_begin_exfiltration_starts_without_vnc_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    created_channel: object | None = None

    class RecordingChannel:
        def __init__(self, *, on_event: object, context: object) -> None:
            nonlocal created_channel
            self.context = context
            self.started = False
            created_channel = self

        async def start(self) -> RecordingChannel:
            self.started = True
            return self

        async def stop(self) -> RecordingChannel:
            return self

    message_channel = _message_channel([BEGIN_EXFILTRATION_DATA, WebSocketDisconnect()])
    browser_session = message_channel.browser_session
    monkeypatch.setattr(message_module, "get_vnc_channel", lambda _client_id: None)
    monkeypatch.setattr(message_module, "get_x_api_key", AsyncMock(return_value="message-api-key"))
    monkeypatch.setattr(message_module, "ExfiltrationChannel", RecordingChannel)
    monkeypatch.setattr(message_module, "interpretation_registry", MagicMock())

    await loop_stream_messages(message_channel)

    assert isinstance(created_channel, RecordingChannel)
    assert created_channel.started is True
    assert created_channel.context.browser_session is browser_session
    assert created_channel.context.x_api_key == "message-api-key"
    assert created_channel.context.organization_id == message_channel.organization_id


def _started_channel(*, stop_error: Exception | None = None) -> MagicMock:
    channel = MagicMock()
    channel.start = AsyncMock(return_value=channel)
    channel.stop = AsyncMock(side_effect=stop_error) if stop_error else AsyncMock(return_value=channel)
    channel.rearm_all_pages = AsyncMock()
    return channel


@pytest.mark.asyncio
async def test_begin_exfiltration_start_failure_does_not_kill_message_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead browser target on recording start must not tear down the websocket loop.

    Mirrors the customer crash-loop: ``connect_over_cdp`` fails (ECONNREFUSED / 502),
    and before the fix the error escaped ``handle_data`` and killed the loop, so the
    frontend reconnected and re-sent begin-exfiltration forever.
    """
    channel = MagicMock()
    channel.start = AsyncMock(
        side_effect=PlaywrightError("BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9222")
    )
    channel.stop = AsyncMock()

    message_channel = _message_channel(
        [BEGIN_EXFILTRATION_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()],
    )
    registry = _install_recording_doubles(monkeypatch, channel=channel)

    await loop_stream_messages(message_channel)

    # The loop consumed every scripted frame instead of dying on the first one.
    assert message_channel.websocket.receive_json.await_count == 3
    # The failure was surfaced to the frontend rather than swallowed silently.
    assert MessageKind.BEGIN_EXFILTRATION.value in _sent_error_kinds(message_channel)
    registry.stop_session.assert_awaited_once_with(PBS_ID)


@pytest.mark.asyncio
async def test_blocked_clipboard_does_not_delay_end_and_is_cancelled_on_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _started_channel()
    message_channel = _message_channel([])
    registry = _install_recording_doubles(monkeypatch, channel=channel)
    clipboard_started = asyncio.Event()
    clipboard_cancelled = asyncio.Event()
    receive_count = 0

    async def receive_json() -> dict[str, str]:
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return BEGIN_EXFILTRATION_DATA
        if receive_count == 2:
            return {"kind": MessageKind.CLIPBOARD_COPY.value}
        if receive_count == 3:
            await clipboard_started.wait()
            return END_EXFILTRATION_DATA
        raise WebSocketDisconnect()

    @asynccontextmanager
    async def blocked_execution(_: object) -> t.AsyncIterator[MagicMock]:
        clipboard_started.set()
        try:
            await asyncio.Event().wait()
            yield MagicMock()
        finally:
            clipboard_cancelled.set()

    message_channel.websocket.receive_json = receive_json
    monkeypatch.setattr(message_module, "execution_for_message_channel", blocked_execution)

    await asyncio.wait_for(loop_stream_messages(message_channel), timeout=2)

    assert receive_count == 4
    channel.stop.assert_awaited_once()
    registry.stop_session.assert_awaited_once_with(PBS_ID)
    assert clipboard_cancelled.is_set()


@pytest.mark.asyncio
async def test_clipboard_task_limit_rejects_third_and_frees_slot_after_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_channel = _message_channel([])
    two_started = asyncio.Event()
    release_first = asyncio.Event()
    first_completed = asyncio.Event()
    slot_reused = asyncio.Event()
    executions_started = 0
    started_at_capacity: list[int] = []
    receive_count = 0

    async def receive_json() -> dict[str, str]:
        nonlocal receive_count
        receive_count += 1
        if receive_count <= 2:
            return {"kind": MessageKind.CLIPBOARD_COPY.value}
        if receive_count == 3:
            await two_started.wait()
            return {"kind": MessageKind.CLIPBOARD_COPY.value}
        if receive_count == 4:
            started_at_capacity.append(executions_started)
            release_first.set()
            await first_completed.wait()
            return {"kind": MessageKind.CLIPBOARD_COPY.value}
        await slot_reused.wait()
        raise WebSocketDisconnect()

    @asynccontextmanager
    async def controlled_execution(_: object) -> t.AsyncIterator[MagicMock]:
        nonlocal executions_started
        execution_index = executions_started
        executions_started += 1
        if executions_started == 2:
            two_started.set()
        elif executions_started == 3:
            slot_reused.set()
        try:
            if execution_index == 0:
                await release_first.wait()
            else:
                await asyncio.Event().wait()
            execute = MagicMock()
            execute.get_selected_text = AsyncMock(return_value=f"copy-{execution_index}")
            yield execute
        finally:
            if execution_index == 0:
                first_completed.set()

    message_channel.websocket.receive_json = receive_json
    message_channel.send_copied_text = AsyncMock()
    monkeypatch.setattr(message_module, "execution_for_message_channel", controlled_execution)

    await asyncio.wait_for(loop_stream_messages(message_channel), timeout=2)

    sent_errors = [
        sent
        for call in message_channel.send_nowait.call_args_list
        for sent in call.kwargs.get("messages", [])
        if isinstance(sent, MessageOutError)
    ]
    assert started_at_capacity == [2]
    assert executions_started == 3
    assert [(error.failed_kind, error.message) for error in sent_errors] == [
        (MessageKind.CLIPBOARD_COPY.value, "Clipboard is busy; try again.")
    ]


@pytest.mark.asyncio
async def test_end_exfiltration_stop_failure_still_flushes_interpretation_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing channel stop must not skip the draft flush (message.py END_EXFILTRATION).

    Mirrors the internal repro: ``stop()`` raised ``TargetClosedError`` and the
    interpretation session was never flushed, so the recording's drafts were lost.
    """
    channel = _started_channel(
        stop_error=PlaywrightError("Page.add_init_script: Target page, context or browser has been closed")
    )
    message_channel = _message_channel(
        [BEGIN_EXFILTRATION_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()],
    )
    registry = _install_recording_doubles(monkeypatch, channel=channel)

    await loop_stream_messages(message_channel)

    assert message_channel.websocket.receive_json.await_count == 3
    registry.stop_session.assert_awaited_once_with(PBS_ID)


@pytest.mark.asyncio
async def test_teardown_stop_failure_still_closes_message_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing channel stop during loop teardown must not skip the websocket close."""
    channel = _started_channel(stop_error=RuntimeError("browser went away mid-recording"))
    # Disconnect while the recording is still active: teardown stops the channel.
    message_channel = _message_channel([BEGIN_EXFILTRATION_DATA, WebSocketDisconnect()])
    _install_recording_doubles(monkeypatch, channel=channel)

    await loop_stream_messages(message_channel)

    channel.stop.assert_awaited()
    message_channel.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_rearm_capture_failure_does_not_kill_message_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing re-arm on an active recording must not tear down the websocket loop."""
    channel = _started_channel()
    channel.rearm_all_pages = AsyncMock(side_effect=RuntimeError("browser went away mid-recording"))

    message_channel = _message_channel(
        [BEGIN_EXFILTRATION_DATA, RECORDING_REARM_CAPTURE_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()],
    )
    registry = _install_recording_doubles(monkeypatch, channel=channel)

    await loop_stream_messages(message_channel)

    assert message_channel.websocket.receive_json.await_count == 4
    assert MessageKind.RECORDING_REARM_CAPTURE.value in _sent_error_kinds(message_channel)
    # The recording still finishes cleanly afterwards.
    registry.stop_session.assert_awaited_once_with(PBS_ID)


@pytest.mark.asyncio
async def test_reconnect_rearm_failure_does_not_kill_message_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing re-arm on a repeated begin-exfiltration (reconnect) must not kill the loop."""
    channel = _started_channel()
    channel.rearm_all_pages = AsyncMock(side_effect=RuntimeError("browser went away mid-recording"))

    # The second begin-exfiltration hits the existing-channel branch, which re-arms.
    message_channel = _message_channel(
        [BEGIN_EXFILTRATION_DATA, BEGIN_EXFILTRATION_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()],
    )
    registry = _install_recording_doubles(monkeypatch, channel=channel)

    await loop_stream_messages(message_channel)

    assert message_channel.websocket.receive_json.await_count == 4
    assert MessageKind.BEGIN_EXFILTRATION.value in _sent_error_kinds(message_channel)
    registry.stop_session.assert_awaited_once_with(PBS_ID)


@pytest.mark.asyncio
async def test_recording_round_trip_still_works_when_target_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guardrails must not change the happy path: start, stop, flush, close."""
    channel = _started_channel()
    message_channel = _message_channel(
        [BEGIN_EXFILTRATION_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()],
    )
    registry = _install_recording_doubles(monkeypatch, channel=channel)

    await loop_stream_messages(message_channel)

    channel.start.assert_awaited_once()
    channel.stop.assert_awaited_once()
    registry.stop_session.assert_awaited_once_with(PBS_ID)
    assert _sent_error_kinds(message_channel) == []
    message_channel.close.assert_awaited_once()


class _FakeV2Session:
    def __init__(self, sealed: bool = False) -> None:
        self.browser_session_id = PBS_ID
        self.sealed = sealed
        self.paused = 0
        self.resumed = 0
        self.seals = 0

    async def seal(self) -> list[object]:
        self.sealed = True
        self.seals += 1
        return []

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1

    async def stop_ticker(self) -> None:
        return None


def _set_record_browser_v2(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    agent_function = MagicMock()
    agent_function.is_record_browser_v2_enabled = AsyncMock(return_value=enabled)
    monkeypatch.setattr(message_module.app, "AGENT_FUNCTION", agent_function)


@pytest.mark.asyncio
async def test_begin_exfiltration_under_v2_starts_a_session_and_injects_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2 owns the recording end to end: no ExfiltrationChannel, so no page scripts."""
    exfiltration_channel_class = MagicMock()
    message_channel = _message_channel([BEGIN_EXFILTRATION_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()])
    monkeypatch.setattr(message_module, "get_vnc_channel", lambda _client_id: MagicMock())
    monkeypatch.setattr(message_module, "ExfiltrationChannel", exfiltration_channel_class)
    registry = MagicMock()
    monkeypatch.setattr(message_module, "interpretation_registry", registry)
    _set_record_browser_v2(monkeypatch, True)

    started: list[str] = []
    session = _FakeV2Session()
    monkeypatch.setattr(
        message_module,
        "get_session_v2",
        lambda pbs_id: session if started else None,
    )
    monkeypatch.setattr(
        message_module,
        "start_session_v2",
        lambda **kwargs: started.append(kwargs["browser_session_id"]),
    )

    await loop_stream_messages(message_channel)

    assert started == [PBS_ID]
    exfiltration_channel_class.assert_not_called()
    registry.start_session.assert_not_called()
    # end-exfiltration seals but keeps the session: its ledger is the commit's only input.
    assert session.seals == 1
    assert _sent_error_kinds(message_channel) == []


RECORDING_COMMIT_DATA = {"kind": MessageKind.RECORDING_COMMIT.value, "mode": "blocks", "draft_steps": None}


def _seed_click_gestures() -> None:
    ledger = get_ledger(PBS_ID)
    assert ledger is not None
    for kind in ("mouse_pressed", "mouse_released"):
        ledger.append(
            Gesture(
                seq=0,
                t_received=1.0,
                kind=kind,
                page_key="page-1",
                url="https://example.test/form",
                x=1,
                y=2,
                button="left",
                click_count=1,
                selector="#search",
                role="button",
                accessible_name="Search",
            )
        )


def _commit_script(commits: int) -> t.Iterator[object]:
    """begin -> record a click -> end -> commit, as the panel drives it."""
    yield BEGIN_EXFILTRATION_DATA
    _seed_click_gestures()
    yield END_EXFILTRATION_DATA
    for _ in range(commits):
        yield RECORDING_COMMIT_DATA
    yield WebSocketDisconnect()


def _committed_messages(message_channel: MagicMock) -> list[MessageOutRecordingCommitted]:
    return [
        sent
        for call in message_channel.send_nowait.call_args_list
        for sent in call.kwargs.get("messages", [])
        if isinstance(sent, MessageOutRecordingCommitted)
    ]


@pytest.mark.asyncio
async def test_recording_commit_renders_the_sealed_session_then_drops_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit rides the messages socket because the ledger is process-local (plan §5 PR-6)."""
    message_channel = _message_channel(_commit_script(commits=2))
    monkeypatch.setattr(message_module, "get_vnc_channel", lambda _client_id: MagicMock())
    monkeypatch.setattr(message_module, "ExfiltrationChannel", MagicMock())
    monkeypatch.setattr(message_module, "interpretation_registry", MagicMock())
    _set_record_browser_v2(monkeypatch, True)

    try:
        await loop_stream_messages(message_channel)
    finally:
        discard_session_v2(PBS_ID)
        stop_ledger(PBS_ID)

    committed = _committed_messages(message_channel)
    assert len(committed) == 1
    assert committed[0].mode == "blocks"
    assert [block["block_type"] for block in committed[0].blocks] == ["action"]
    # The committed recording is gone: the second commit finds nothing to render.
    assert get_session_v2(PBS_ID) is None
    assert get_ledger(PBS_ID) is None
    assert _sent_error_kinds(message_channel) == [MessageKind.RECORDING_COMMIT.value]


@pytest.mark.asyncio
async def test_v2_teardown_still_finds_the_session_after_browser_session_goes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """message_channel.browser_session can flip to None mid-recording (loop_verify_browser_session
    clears it on timeout/finalize without closing the socket). Teardown must still locate and
    seal the live v2 session instead of leaking it forever."""
    message_channel = _message_channel([BEGIN_EXFILTRATION_DATA, WebSocketDisconnect()])
    monkeypatch.setattr(message_module, "get_vnc_channel", lambda _client_id: MagicMock())
    monkeypatch.setattr(message_module, "ExfiltrationChannel", MagicMock())
    monkeypatch.setattr(message_module, "interpretation_registry", MagicMock())
    _set_record_browser_v2(monkeypatch, True)

    session = _FakeV2Session(sealed=True)
    monkeypatch.setattr(message_module, "get_session_v2", lambda pbs_id: session)
    monkeypatch.setattr(
        message_module, "start_session_v2", lambda **kwargs: message_channel.__setattr__("browser_session", None)
    )
    discarded: list[str] = []
    monkeypatch.setattr(message_module, "discard_session_v2", lambda pbs_id: discarded.append(pbs_id))

    await loop_stream_messages(message_channel)

    assert discarded == [PBS_ID]


@pytest.mark.asyncio
async def test_begin_exfiltration_with_v2_disabled_takes_the_v1_path(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _started_channel()
    message_channel = _message_channel([BEGIN_EXFILTRATION_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()])
    registry = _install_recording_doubles(monkeypatch, channel=channel)
    _set_record_browser_v2(monkeypatch, False)

    started: list[str] = []
    monkeypatch.setattr(message_module, "start_session_v2", lambda **kwargs: started.append("v2"))

    await loop_stream_messages(message_channel)

    assert started == []
    channel.start.assert_awaited_once()
    registry.start_session.assert_called_once()
    registry.stop_session.assert_awaited_once_with(PBS_ID)


@pytest.mark.asyncio
async def test_stale_v2_session_does_not_hijack_a_v1_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """The socket latches its recorder at begin-exfiltration; the v2 registry is not the switch."""
    channel = _started_channel()
    message_channel = _message_channel([BEGIN_EXFILTRATION_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()])
    registry = _install_recording_doubles(monkeypatch, channel=channel)
    _set_record_browser_v2(monkeypatch, False)

    stale = _FakeV2Session()
    monkeypatch.setattr(message_module, "get_session_v2", lambda pbs_id: stale)

    await loop_stream_messages(message_channel)

    assert stale.seals == 0
    channel.stop.assert_awaited_once()
    registry.stop_session.assert_awaited_once_with(PBS_ID)


@pytest.mark.asyncio
async def test_a_mid_socket_flag_flip_does_not_start_a_second_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = _started_channel()
    message_channel = _message_channel(
        [BEGIN_EXFILTRATION_DATA, BEGIN_EXFILTRATION_DATA, END_EXFILTRATION_DATA, WebSocketDisconnect()]
    )
    registry = _install_recording_doubles(monkeypatch, channel=channel)

    agent_function = MagicMock()
    agent_function.is_record_browser_v2_enabled = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(message_module.app, "AGENT_FUNCTION", agent_function)
    started: list[str] = []
    monkeypatch.setattr(message_module, "start_session_v2", lambda **kwargs: started.append("v2"))

    await loop_stream_messages(message_channel)

    assert started == []
    assert agent_function.is_record_browser_v2_enabled.await_count == 1
    message_module.ExfiltrationChannel.assert_called_once()
    channel.rearm_all_pages.assert_awaited_once()
    registry.stop_session.assert_awaited_once_with(PBS_ID)


@pytest.mark.asyncio
async def test_v2_recording_reaches_the_panel_as_epoch_stamped_draft_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end over the real v2 session: a ledger row becomes a panel-ready draft step."""
    message_channel = _message_channel([])
    monkeypatch.setattr(message_module, "get_vnc_channel", lambda _client_id: MagicMock())
    monkeypatch.setattr(message_module, "ExfiltrationChannel", MagicMock())
    monkeypatch.setattr(message_module, "interpretation_registry", MagicMock())
    _set_record_browser_v2(monkeypatch, True)
    receive_count = 0

    async def receive_json() -> dict[str, str]:
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return BEGIN_EXFILTRATION_DATA
        if receive_count == 2:
            ledger = get_ledger(PBS_ID)
            assert ledger is not None
            ledger.append(
                Gesture(
                    seq=0,
                    t_received=time.monotonic(),
                    kind="mouse_pressed",
                    page_key="page-1",
                    url="https://example.test/form",
                    x=1,
                    y=2,
                    button="left",
                    click_count=1,
                    accessible_name="Search",
                )
            )
            ledger.append(
                Gesture(
                    seq=0,
                    t_received=time.monotonic(),
                    kind="key",
                    page_key="page-1",
                    url="https://example.test/form",
                    key="s",
                    text="hunter2",
                    key_event_type="keyDown",
                    accessible_name="Search",
                )
            )
            return END_EXFILTRATION_DATA
        raise WebSocketDisconnect()

    message_channel.websocket.receive_json = receive_json

    try:
        await loop_stream_messages(message_channel)
    finally:
        discard_session_v2(PBS_ID)
        stop_ledger(PBS_ID)

    updates = [
        sent
        for call in message_channel.send_nowait.call_args_list
        for sent in call.kwargs.get("messages", [])
        if isinstance(sent, message_module.MessageOutRecordingInterpretationUpdate)
    ]
    assert updates[-1].finalized is True
    steps = updates[-1].steps
    assert [(step.action_kind, step.label, step.navigation_goal) for step in steps] == [
        (ActionKind.CLICK, "Click Search", "Click Search"),
        (ActionKind.INPUT_TEXT, "Type into Search", "Type into Search"),
    ]
    now_epoch_ms = time.time() * 1000
    assert all(abs(now_epoch_ms - (step.timestamp_start or 0)) < 60_000 for step in steps)
    assert "hunter2" not in json.dumps([step.model_dump() for step in steps])
