"""Tests for `reify_channel_message` and lightweight ExecutionChannel helpers."""

from __future__ import annotations

import typing as t
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

from skyvern.config import settings
from skyvern.forge.sdk.routes.streaming.channels import message as message_module
from skyvern.forge.sdk.routes.streaming.channels.execution import ExecutionChannel, LocalExecutionChannel
from skyvern.forge.sdk.routes.streaming.channels.message import (
    MessageChannel,
    MessageInClearAllData,
    MessageInClearCookies,
    MessageInClearHistory,
    MessageInClipboardPaste,
    MessageInGoBack,
    MessageInGoForward,
    MessageInNavigate,
    MessageInReload,
    MessageInTakeScreenshot,
    MessageKind,
    loop_stream_messages,
    reify_channel_message,
)


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
        page.evaluate.assert_awaited_once_with("() => 'hello'", None)

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
        websocket = MagicMock()
        websocket.client_state = WebSocketState.CONNECTED
        websocket.send_json = AsyncMock()
        websocket.close = AsyncMock()

        message_channel = MessageChannel(
            client_id="client-1",
            organization_id="org-1",
            websocket=websocket,
        )

        drain_calls = 0

        async def drain_once_then_close() -> list[dict[str, object]]:
            nonlocal drain_calls
            drain_calls += 1
            if drain_calls == 1:
                return [payload]
            websocket.client_state = WebSocketState.DISCONNECTED
            return []

        message_channel.drain = drain_once_then_close  # type: ignore[method-assign]

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

        await loop_stream_messages(message_channel)

        method = getattr(execute, method_name)
        method.assert_awaited_once_with(**expected_kwargs)
        execute.get_current_url.assert_awaited_once_with()
        websocket.send_json.assert_any_await({"kind": "browser-url", "url": current_url})
