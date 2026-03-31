from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState
from skyvern.cli.mcp_tools.inspection import (
    _redact_url,
    _register_hooks_on_page,
    skyvern_console_messages,
    skyvern_handle_dialog,
    skyvern_network_requests,
)


def _make_state() -> SessionState:
    return SessionState(
        console_messages=deque(maxlen=1000),
        network_requests=deque(maxlen=1000),
        dialog_events=deque(maxlen=1000),
    )


def _make_page(raw: MagicMock | None = None) -> SimpleNamespace:
    if raw is None:
        raw = MagicMock()
        raw.on = MagicMock()
    return SimpleNamespace(page=raw)


def _patch(monkeypatch: pytest.MonkeyPatch, state: SessionState) -> None:
    raw = MagicMock()
    raw.on = MagicMock()

    async def fake_get_page(**kwargs):
        return _make_page(raw), BrowserContext(mode="local")

    monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_page", fake_get_page)
    monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_current_session", lambda: state)
    # Reset stateless HTTP mode — cloud_app.py sets this to True at startup when MCP_ENABLED,
    # which causes inspection tools to short-circuit with ACTION_FAILED before reaching test logic.
    monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", False)


def _console_entry(level: str = "log", text: str = "msg") -> dict:
    return {"level": level, "text": text, "timestamp": 1.0, "source_url": "", "page_url": "", "line_number": 0}


def _network_entry(url: str = "https://a.com", method: str = "GET", status: int = 200) -> dict:
    return {"url": url, "method": method, "status": status, "content_type": "", "timing_ms": 0, "response_size": 0}


# --- Hook registration ---


class TestEnsureHooks:
    def test_registers_three_listeners(self) -> None:
        state = _make_state()
        raw = MagicMock()
        raw.on = MagicMock()
        _register_hooks_on_page(state, raw)
        assert raw.on.call_count == 3
        assert {c.args[0] for c in raw.on.call_args_list} == {"console", "response", "dialog"}

    def test_idempotent(self) -> None:
        state = _make_state()
        raw = MagicMock()
        raw.on = MagicMock()
        _register_hooks_on_page(state, raw)
        _register_hooks_on_page(state, raw)
        assert raw.on.call_count == 3

    def test_keeps_hooks_on_both_pages(self) -> None:
        """Multi-page: hooks are registered on ALL pages, not removed on switch."""
        state = _make_state()
        raw1 = MagicMock()
        raw1.on = MagicMock()
        raw1.remove_listener = MagicMock()
        raw2 = MagicMock()
        raw2.on = MagicMock()
        _register_hooks_on_page(state, raw1)
        _register_hooks_on_page(state, raw2)
        # Both pages should have hooks registered — no removal
        assert raw1.remove_listener.call_count == 0
        assert raw1.on.call_count == 3
        assert raw2.on.call_count == 3


# --- Console messages ---


class TestConsoleMessages:
    @pytest.mark.asyncio
    async def test_returns_and_filters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        state.console_messages.append(_console_entry("log", "hello"))
        state.console_messages.append(_console_entry("error", "fail"))
        _patch(monkeypatch, state)

        all_result = await skyvern_console_messages()
        assert all_result["data"]["count"] == 2

        by_level = await skyvern_console_messages(level="error")
        assert by_level["data"]["count"] == 1

        by_text = await skyvern_console_messages(text="hel")
        assert by_text["data"]["count"] == 1

    @pytest.mark.asyncio
    async def test_clear_with_filter_preserves_unmatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        state.console_messages.append(_console_entry("error", "fail"))
        state.console_messages.append(_console_entry("log", "keep"))
        _patch(monkeypatch, state)

        result = await skyvern_console_messages(level="error", clear=True)
        assert result["data"]["count"] == 1
        assert len(state.console_messages) == 1
        assert state.console_messages[0]["text"] == "keep"

    @pytest.mark.asyncio
    async def test_no_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.core.session_manager import BrowserNotAvailableError

        async def raise_err(**kw):
            raise BrowserNotAvailableError()

        monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_page", raise_err)
        result = await skyvern_console_messages()
        assert result["ok"] is False


# --- Network requests ---


class TestNetworkRequests:
    @pytest.mark.asyncio
    async def test_returns_and_filters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry("https://api.com/v1", "GET", 200))
        state.network_requests.append(_network_entry("https://cdn.com/img.png", "POST", 404))
        _patch(monkeypatch, state)

        by_url = await skyvern_network_requests(url_pattern="api")
        assert by_url["data"]["count"] == 1

        by_status = await skyvern_network_requests(status_code=404)
        assert by_status["data"]["count"] == 1

        by_method = await skyvern_network_requests(method="post")
        assert by_method["data"]["count"] == 1

    @pytest.mark.asyncio
    async def test_invalid_regex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, _make_state())
        result = await skyvern_network_requests(url_pattern="[invalid")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"


# --- Dialog ---


class TestDialog:
    @pytest.mark.asyncio
    async def test_returns_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        state.dialog_events.append(
            {"type": "alert", "message": "Hi", "default_value": None, "action_taken": "dismissed", "timestamp": 1.0}
        )
        _patch(monkeypatch, state)
        result = await skyvern_handle_dialog()
        assert result["data"]["count"] == 1


# --- URL redaction ---


@pytest.mark.parametrize(
    "url,expected_missing",
    [
        ("https://a.com/path", None),  # no params — unchanged
        ("https://a.com?q=hello", None),  # safe param — unchanged
        ("https://a.com?token=secret123", "secret123"),
        ("https://s3.aws.com/obj?X-Amz-Signature=abc", "abc"),
        ("https://a.com?api_key=my-key&page=1", "my-key"),
    ],
)
def test_redact_url(url: str, expected_missing: str | None) -> None:
    result = _redact_url(url)
    if expected_missing is None:
        assert result == url
    else:
        assert expected_missing not in result


# --- Stateless HTTP error ---


class TestStatelessError:
    @pytest.mark.asyncio
    async def test_all_tools_error_in_stateless_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, _make_state())
        monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", True)

        for tool in (skyvern_console_messages, skyvern_network_requests, skyvern_handle_dialog):
            result = await tool()
            assert result["ok"] is False, f"{tool.__name__} should error in stateless mode"


# --- Deque eviction ---


def test_deque_evicts_oldest() -> None:
    state = _make_state()
    for i in range(1001):
        state.console_messages.append(_console_entry(text=f"msg-{i}"))
    assert len(state.console_messages) == 1000
    assert state.console_messages[0]["text"] == "msg-1"
