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
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect
from playwright._impl._errors import Error as PlaywrightError
from starlette.websockets import WebSocketState

from skyvern.forge.sdk.routes.streaming.channels import message as message_module
from skyvern.forge.sdk.routes.streaming.channels.message import MessageKind, MessageOutError, loop_stream_messages

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


def _message_channel(receive_sequence: list[object]) -> MagicMock:
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
    monkeypatch.setattr(message_module, "get_vnc_channel", lambda _client_id: MagicMock())
    monkeypatch.setattr(message_module, "ExfiltrationChannel", MagicMock(return_value=channel))

    registry = MagicMock()
    registry.start_session = MagicMock()
    registry.ingest_events = MagicMock()
    registry.stop_session = AsyncMock(return_value=[])
    monkeypatch.setattr(message_module, "interpretation_registry", registry)
    return registry


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
    _install_recording_doubles(monkeypatch, channel=channel)

    await loop_stream_messages(message_channel)

    # The loop consumed every scripted frame instead of dying on the first one.
    assert message_channel.websocket.receive_json.await_count == 3
    # The failure was surfaced to the frontend rather than swallowed silently.
    assert MessageKind.BEGIN_EXFILTRATION.value in _sent_error_kinds(message_channel)


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
