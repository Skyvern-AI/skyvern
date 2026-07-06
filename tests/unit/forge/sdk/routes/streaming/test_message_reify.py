"""Tests for `reify_channel_message` and lightweight ExecutionChannel helpers."""

from __future__ import annotations

import asyncio
import json
import typing as t
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

from skyvern.config import settings
from skyvern.forge.sdk.routes.streaming.channels import message as message_module
from skyvern.forge.sdk.routes.streaming.channels.execution import ExecutionChannel, LocalExecutionChannel
from skyvern.forge.sdk.routes.streaming.channels.message import (
    MessageChannel,
    MessageInBeginExfiltration,
    MessageInClearAllData,
    MessageInClearCookies,
    MessageInClearHistory,
    MessageInClipboardPaste,
    MessageInGoBack,
    MessageInGoForward,
    MessageInNavigate,
    MessageInRecordingRearmCapture,
    MessageInReload,
    MessageInTakeScreenshot,
    MessageKind,
    MessageOutBrowserUrl,
    MessageOutExfiltratedEvent,
    MessageOutRecordingInterpretationUpdate,
    loop_stream_messages,
    message_to_dict,
    reify_channel_message,
)
from skyvern.services.browser_recording.types import ActionKind, RecordingDraftStep


class TestReifyChannelMessage:
    def test_navigate_with_url(self) -> None:
        msg = reify_channel_message({"kind": "navigate", "url": "https://example.com"})
        assert isinstance(msg, MessageInNavigate)
        assert msg.url == "https://example.com"
        assert msg.kind == MessageKind.NAVIGATE

    def test_navigate_missing_url_defaults_to_empty(self) -> None:
        msg = reify_channel_message({"kind": "navigate"})
        assert isinstance(msg, MessageInNavigate)
        assert msg.url == ""

    def test_reload_soft(self) -> None:
        msg = reify_channel_message({"kind": "reload", "hard": False})
        assert isinstance(msg, MessageInReload)
        assert msg.hard is False

    def test_reload_hard(self) -> None:
        msg = reify_channel_message({"kind": "reload", "hard": True})
        assert isinstance(msg, MessageInReload)
        assert msg.hard is True

    def test_reload_default_is_soft(self) -> None:
        msg = reify_channel_message({"kind": "reload"})
        assert isinstance(msg, MessageInReload)
        assert msg.hard is False

    def test_go_back(self) -> None:
        assert isinstance(reify_channel_message({"kind": "go-back"}), MessageInGoBack)

    def test_go_forward(self) -> None:
        assert isinstance(reify_channel_message({"kind": "go-forward"}), MessageInGoForward)

    def test_take_screenshot(self) -> None:
        assert isinstance(
            reify_channel_message({"kind": "take-screenshot"}),
            MessageInTakeScreenshot,
        )

    def test_clear_cookies(self) -> None:
        assert isinstance(reify_channel_message({"kind": "clear-cookies"}), MessageInClearCookies)

    def test_clear_history(self) -> None:
        assert isinstance(reify_channel_message({"kind": "clear-history"}), MessageInClearHistory)

    def test_clear_all_data(self) -> None:
        assert isinstance(reify_channel_message({"kind": "clear-all-data"}), MessageInClearAllData)

    def test_clipboard_paste_carries_text(self) -> None:
        msg = reify_channel_message({"kind": "clipboard-paste", "text": "hello"})
        assert isinstance(msg, MessageInClipboardPaste)
        assert msg.text == "hello"

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError):
            reify_channel_message({"kind": "totally-not-a-real-kind"})

    def test_begin_exfiltration_carries_live_interpretation_context(self) -> None:
        msg = reify_channel_message(
            {
                "kind": "begin-exfiltration",
                "workflow_permanent_id": "wpid_123",
                "live_interpretation_enabled": True,
            }
        )

        assert isinstance(msg, MessageInBeginExfiltration)
        assert msg.workflow_permanent_id == "wpid_123"
        assert msg.live_interpretation_enabled is True

    def test_begin_exfiltration_defaults_no_delta_support(self) -> None:
        # An existing client that predates deltas omits the field; it must default
        # false so the backend keeps sending full snapshots (no breakage).
        msg = reify_channel_message(
            {
                "kind": "begin-exfiltration",
                "workflow_permanent_id": "wpid_123",
                "live_interpretation_enabled": True,
            }
        )
        assert isinstance(msg, MessageInBeginExfiltration)
        assert msg.supports_interpretation_deltas is False

    def test_begin_exfiltration_honors_declared_delta_support(self) -> None:
        msg = reify_channel_message(
            {
                "kind": "begin-exfiltration",
                "workflow_permanent_id": "wpid_123",
                "live_interpretation_enabled": True,
                "supports_interpretation_deltas": True,
            }
        )
        assert isinstance(msg, MessageInBeginExfiltration)
        assert msg.supports_interpretation_deltas is True

    def test_begin_exfiltration_defaults_no_recording_attempt_id(self) -> None:
        msg = reify_channel_message(
            {
                "kind": "begin-exfiltration",
                "workflow_permanent_id": "wpid_123",
                "live_interpretation_enabled": True,
            }
        )
        assert isinstance(msg, MessageInBeginExfiltration)
        assert msg.recording_attempt_id is None

    def test_begin_exfiltration_carries_recording_attempt_id(self) -> None:
        msg = reify_channel_message(
            {
                "kind": "begin-exfiltration",
                "workflow_permanent_id": "wpid_123",
                "live_interpretation_enabled": True,
                "recording_attempt_id": "attempt-abc",
            }
        )
        assert isinstance(msg, MessageInBeginExfiltration)
        assert msg.recording_attempt_id == "attempt-abc"

    def test_recording_rearm_capture(self) -> None:
        assert isinstance(
            reify_channel_message({"kind": "recording-rearm-capture"}),
            MessageInRecordingRearmCapture,
        )


class TestMessageSerialization:
    def test_recording_interpretation_update_serializes_pydantic_steps(self) -> None:
        message = MessageOutRecordingInterpretationUpdate(
            interpretation_session_id="session-abc",
            session_revision=2,
            pending=False,
            finalized=True,
            steps=[
                RecordingDraftStep(
                    step_id="step-1",
                    action_kind=ActionKind.CLICK,
                    block_type="action",
                    label="click_submit",
                    title="Click submit",
                    navigation_goal="Click submit",
                )
            ],
        )

        serialized = message_to_dict(message)

        assert serialized["kind"] == "recording-interpretation-update"
        assert serialized["interpretation_session_id"] == "session-abc"
        assert serialized["session_revision"] == 2
        assert serialized["finalized"] is True
        assert serialized["steps"][0]["action_kind"] == "click"


class TestExecutionChannelHelpers:
    @pytest.mark.asyncio
    async def test_get_current_url_does_not_collide_with_cdp_url_identity(self) -> None:
        vnc_channel = MagicMock()
        vnc_channel.identity = {"client_id": "abc"}

        page = MagicMock()
        page.url = "https://example.com/current"

        channel = ExecutionChannel(vnc_channel=vnc_channel)
        channel.url = "ws://cdp.example/devtools/browser/123"
        channel.page = page

        assert await channel.get_current_url() == "https://example.com/current"
        assert channel.identity["cdp_url"] == "ws://cdp.example/devtools/browser/123"
        assert "url" not in channel.identity

    def test_normalize_url_passes_https(self) -> None:
        assert ExecutionChannel._normalize_url("https://example.com/path") == "https://example.com/path"

    def test_normalize_url_passes_http(self) -> None:
        assert ExecutionChannel._normalize_url("http://example.com") == "http://example.com"

    def test_normalize_url_prepends_https_for_bare_host(self) -> None:
        assert ExecutionChannel._normalize_url("example.com") == "https://example.com"

    def test_normalize_url_strips_whitespace(self) -> None:
        assert ExecutionChannel._normalize_url("  https://example.com  ") == "https://example.com"

    def test_normalize_url_rejects_non_http_scheme(self) -> None:
        with pytest.raises(ValueError):
            ExecutionChannel._normalize_url("javascript:alert(1)")

    def test_normalize_url_rejects_file_scheme(self) -> None:
        with pytest.raises(ValueError):
            ExecutionChannel._normalize_url("file:///etc/passwd")

    @pytest.mark.parametrize("url", ["", "   "])
    def test_normalize_url_rejects_empty_url(self, url: str) -> None:
        with pytest.raises(ValueError, match="URL must not be empty"):
            ExecutionChannel._normalize_url(url)

    def test_normalize_url_rejects_protocol_relative_url(self) -> None:
        with pytest.raises(ValueError, match="explicit http\\(s\\) scheme or host"):
            ExecutionChannel._normalize_url("//evil.com")

    def test_origin_of_extracts_origin(self) -> None:
        assert ExecutionChannel._origin_of("https://example.com/foo/bar?q=1") == "https://example.com"

    def test_origin_of_includes_port(self) -> None:
        assert ExecutionChannel._origin_of("http://localhost:8080/x") == "http://localhost:8080"

    def test_origin_of_returns_empty_for_blank(self) -> None:
        assert ExecutionChannel._origin_of("") == ""

    def test_origin_of_returns_empty_for_about_blank(self) -> None:
        # No scheme/netloc on "about:blank" -- treat as empty so CDP doesn't choke.
        assert ExecutionChannel._origin_of("about:blank") == ""

    @pytest.mark.asyncio
    async def test_navigation_helpers_use_action_timeout(self) -> None:
        vnc_channel = MagicMock()
        vnc_channel.identity = {"client_id": "abc"}

        page = MagicMock()
        page.url = "https://example.com"
        page.goto = AsyncMock()
        page.reload = AsyncMock()
        page.go_back = AsyncMock()
        page.go_forward = AsyncMock()
        page.context.new_cdp_session = AsyncMock()

        channel = ExecutionChannel(vnc_channel=vnc_channel)
        channel.page = page

        await channel.navigate("example.com")
        await channel.reload()
        await channel.go_back()
        await channel.go_forward()

        timeout_kwargs = {
            "wait_until": "domcontentloaded",
            "timeout": settings.BROWSER_ACTION_TIMEOUT_MS,
        }
        page.goto.assert_awaited_once_with("https://example.com", **timeout_kwargs)
        page.reload.assert_awaited_once_with(**timeout_kwargs)
        page.go_back.assert_awaited_once_with(**timeout_kwargs)
        page.go_forward.assert_awaited_once_with(**timeout_kwargs)


class TestLocalExecutionChannel:
    @pytest.mark.asyncio
    async def test_evaluate_js_calls_page_evaluate_directly(self) -> None:
        page = MagicMock()
        page.url = "https://example.com"
        page.context = MagicMock()
        page.evaluate = AsyncMock(return_value="hello")

        channel = LocalExecutionChannel(page=page)
        result = await channel.evaluate_js("() => 'hello'")

        assert result == "hello"
        page.evaluate.assert_awaited_once_with("() => 'hello'")

    @pytest.mark.asyncio
    async def test_evaluate_js_forwards_arg(self) -> None:
        page = MagicMock()
        page.url = "https://example.com"
        page.context = MagicMock()
        page.evaluate = AsyncMock(return_value=None)

        channel = LocalExecutionChannel(page=page)
        await channel.evaluate_js("(text) => text", "pasted")

        page.evaluate.assert_awaited_once_with("(text) => text", "pasted")

    @pytest.mark.asyncio
    async def test_paste_text_uses_keyboard_insert_text(self) -> None:
        page = MagicMock()
        page.url = "https://example.com"
        page.context = MagicMock()
        page.keyboard.insert_text = AsyncMock()
        page.evaluate = AsyncMock()

        channel = LocalExecutionChannel(page=page)
        await channel.paste_text("pasted")

        page.keyboard.insert_text.assert_awaited_once_with("pasted")
        page.evaluate.assert_not_awaited()


def _make_loop_websocket() -> MagicMock:
    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()
    websocket.close = AsyncMock()
    return websocket


class TestMessageLoopUrlUpdates:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "method_name", "expected_kwargs", "current_url"),
        [
            ({"kind": "go-back"}, "go_back", {}, "https://example.com/previous"),
            ({"kind": "go-forward"}, "go_forward", {}, "https://example.com/next"),
            ({"kind": "reload", "hard": True}, "reload", {"hard": True}, "https://example.com/redirected"),
        ],
    )
    async def test_browser_history_actions_push_current_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: dict[str, object],
        method_name: str,
        expected_kwargs: dict[str, object],
        current_url: str,
    ) -> None:
        websocket = _make_loop_websocket()
        url_sent = asyncio.Event()

        async def send_json(data: dict[str, object]) -> None:
            if data.get("kind") == "browser-url":
                url_sent.set()

        websocket.send_json = AsyncMock(side_effect=send_json)

        receive_calls = 0

        async def receive_json() -> dict[str, object]:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return payload
            # Hold the reader open until the pump has flushed the queued
            # response, then end the session.
            await url_sent.wait()
            raise WebSocketDisconnect(code=1000)

        websocket.receive_json = receive_json

        message_channel = MessageChannel(
            client_id="client-1",
            organization_id="org-1",
            websocket=websocket,
        )

        execute = AsyncMock()
        execute.get_current_url = AsyncMock(return_value=current_url)

        @asynccontextmanager
        async def fake_execution_for_message_channel(_: MessageChannel) -> t.AsyncIterator[AsyncMock]:
            yield execute

        monkeypatch.setattr(
            message_module,
            "execution_for_message_channel",
            fake_execution_for_message_channel,
        )

        await asyncio.wait_for(loop_stream_messages(message_channel), timeout=2)

        method = getattr(execute, method_name)
        method.assert_awaited_once_with(**expected_kwargs)
        execute.get_current_url.assert_awaited_once_with()
        websocket.send_json.assert_any_await({"kind": "browser-url", "url": current_url})


class TestMessageLoopPump:
    @pytest.mark.asyncio
    async def test_outbound_pump_sends_queued_message_without_inbound_traffic(self) -> None:
        # Regression test for the old busy-poll drain: a queued MessageOut must be
        # written by the pump waking on put, with zero inbound traffic driving it.
        websocket = _make_loop_websocket()
        url_sent = asyncio.Event()
        release_reader = asyncio.Event()

        async def send_json(data: dict[str, object]) -> None:
            if data.get("kind") == "browser-url":
                url_sent.set()

        websocket.send_json = AsyncMock(side_effect=send_json)

        async def receive_json() -> dict[str, object]:
            await release_reader.wait()
            raise WebSocketDisconnect(code=1000)

        websocket.receive_json = receive_json

        message_channel = MessageChannel(
            client_id="client-1",
            organization_id="org-1",
            websocket=websocket,
        )

        loop_task = asyncio.create_task(loop_stream_messages(message_channel))
        try:
            message_channel.send_nowait(messages=[MessageOutBrowserUrl(url="https://example.com/queued")])
            await asyncio.wait_for(url_sent.wait(), timeout=2)
        finally:
            release_reader.set()
            await asyncio.wait_for(loop_task, timeout=2)

        websocket.send_json.assert_any_await({"kind": "browser-url", "url": "https://example.com/queued"})

    @pytest.mark.asyncio
    async def test_pump_ends_session_on_non_disconnect_send_failure(self) -> None:
        # A non-WebSocketDisconnect send failure means the socket is unhealthy
        # (e.g. Starlette's RuntimeError after a close frame); the pump must end
        # the session rather than dequeue-log-repeat per message.
        websocket = _make_loop_websocket()
        websocket.send_json = AsyncMock(side_effect=RuntimeError("send after close"))
        release_reader = asyncio.Event()

        async def receive_json() -> dict[str, object]:
            await release_reader.wait()
            raise WebSocketDisconnect(code=1000)

        websocket.receive_json = receive_json

        message_channel = MessageChannel(
            client_id="client-1",
            organization_id="org-1",
            websocket=websocket,
        )

        message_channel.send_nowait(messages=[MessageOutBrowserUrl(url="https://example.com/poison")])

        # The pump's send failure alone must tear the session down — the reader
        # is never released.
        await asyncio.wait_for(loop_stream_messages(message_channel), timeout=2)

        websocket.send_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_exits_cleanly_while_pump_idle(self) -> None:
        # The pump blocks on out_queue.get(); a client disconnect must cancel it
        # and return promptly instead of hanging the session task.
        websocket = _make_loop_websocket()

        async def receive_json() -> dict[str, object]:
            raise WebSocketDisconnect(code=1001)

        websocket.receive_json = receive_json

        message_channel = MessageChannel(
            client_id="client-1",
            organization_id="org-1",
            websocket=websocket,
        )

        await asyncio.wait_for(loop_stream_messages(message_channel), timeout=2)

        websocket.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_outbound_flows_while_inbound_handler_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Head-of-line fix: an out_queue message must reach the client while the
        # reader is still awaiting a slow inbound command handler.
        websocket = _make_loop_websocket()
        handler_started = asyncio.Event()
        handler_gate = asyncio.Event()
        url_sent = asyncio.Event()

        async def send_json(data: dict[str, object]) -> None:
            if data.get("kind") == "browser-url":
                url_sent.set()

        websocket.send_json = AsyncMock(side_effect=send_json)

        receive_calls = 0

        async def receive_json() -> dict[str, object]:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"kind": "take-screenshot"}
            raise WebSocketDisconnect(code=1000)

        websocket.receive_json = receive_json

        message_channel = MessageChannel(
            client_id="client-1",
            organization_id="org-1",
            websocket=websocket,
        )

        async def slow_screenshot() -> str:
            handler_started.set()
            await handler_gate.wait()
            return "b64-screenshot"

        execute = AsyncMock()
        execute.take_screenshot = slow_screenshot

        @asynccontextmanager
        async def fake_execution_for_message_channel(_: MessageChannel) -> t.AsyncIterator[AsyncMock]:
            yield execute

        monkeypatch.setattr(
            message_module,
            "execution_for_message_channel",
            fake_execution_for_message_channel,
        )

        loop_task = asyncio.create_task(loop_stream_messages(message_channel))
        try:
            await asyncio.wait_for(handler_started.wait(), timeout=2)
            # Reader is parked inside the screenshot handler; the pump must
            # still deliver this immediately.
            message_channel.send_nowait(messages=[MessageOutBrowserUrl(url="https://example.com/live")])
            await asyncio.wait_for(url_sent.wait(), timeout=2)
            assert not handler_gate.is_set()
        finally:
            handler_gate.set()
            await asyncio.wait_for(loop_task, timeout=2)

        websocket.send_json.assert_any_await({"kind": "browser-url", "url": "https://example.com/live"})

    @pytest.mark.asyncio
    async def test_external_cancellation_does_not_strand_pump(self) -> None:
        # collect() cancels loop_stream_messages when a sibling loop (e.g. verify)
        # raises. That cancellation lands mid-await on asyncio.wait, which does not
        # cancel the tasks it awaits — the teardown must cancel both loops in its
        # finally so backend_to_frontend isn't left blocked on out_queue.get().
        websocket = _make_loop_websocket()
        reader_started = asyncio.Event()

        async def receive_json() -> dict[str, object]:
            reader_started.set()
            await asyncio.Event().wait()  # block forever; out_queue is also empty

        websocket.receive_json = receive_json

        message_channel = MessageChannel(
            client_id="client-1",
            organization_id="org-1",
            websocket=websocket,
        )

        loop_task = asyncio.create_task(loop_stream_messages(message_channel))
        await asyncio.wait_for(reader_started.wait(), timeout=2)

        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(loop_task, timeout=2)

        # By the time the cancellation propagates out, the finally has cancelled
        # and gathered both child loops — nothing left running.
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert leftover == []
        websocket.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_clipboard_frames_go_through_the_pump(self) -> None:
        # Sole-writer invariant: clipboard sends (called from the VNC task) must
        # enqueue and be written by the pump, never write the websocket directly.
        websocket = _make_loop_websocket()
        copied_sent = asyncio.Event()
        release_reader = asyncio.Event()

        async def send_json(data: dict[str, object]) -> None:
            if data.get("kind") == "copied-text":
                copied_sent.set()

        websocket.send_json = AsyncMock(side_effect=send_json)

        async def receive_json() -> dict[str, object]:
            await release_reader.wait()
            raise WebSocketDisconnect(code=1000)

        websocket.receive_json = receive_json

        message_channel = MessageChannel(
            client_id="client-1",
            organization_id="org-1",
            websocket=websocket,
        )

        loop_task = asyncio.create_task(loop_stream_messages(message_channel))
        try:
            await message_channel.send_copied_text("hello clipboard")
            await asyncio.wait_for(copied_sent.wait(), timeout=2)
        finally:
            release_reader.set()
            await asyncio.wait_for(loop_task, timeout=2)

        websocket.send_json.assert_any_await({"kind": "copied-text", "text": "hello clipboard"})


class TestMessageToDictJsonSafety:
    def test_non_finite_floats_become_null(self) -> None:
        msg = MessageOutExfiltratedEvent(
            event_name="click",
            params={"mousePosition": {"xa": 42.0, "xp": float("nan"), "yp": float("inf")}},
        )

        data = message_to_dict(msg)

        mouse = data["params"]["mousePosition"]
        assert mouse["xa"] == 42.0
        assert mouse["xp"] is None
        assert mouse["yp"] is None
        json.dumps(data, allow_nan=False)

    def test_non_finite_floats_in_nested_model_become_null(self) -> None:
        step = RecordingDraftStep(
            step_id="s1",
            action_kind=ActionKind.CLICK,
            block_type="action",
            label="click",
            timestamp_start=float("nan"),
        )
        msg = MessageOutRecordingInterpretationUpdate(steps=[step])

        data = message_to_dict(msg)

        assert data["steps"][0]["timestamp_start"] is None
        json.dumps(data, allow_nan=False)
