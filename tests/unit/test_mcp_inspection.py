from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import RequestFailureLog, SessionState
from skyvern.cli.mcp_tools.inspection import (
    _redact_url,
    _register_hooks_on_page,
    skyvern_console_messages,
    skyvern_handle_dialog,
    skyvern_network_requests,
)
from tests.unit._mcp_browser_fakes import logged_request_failures
from tests.unit._mcp_browser_fakes import make_page as _make_page
from tests.unit._mcp_browser_fakes import make_session_state as _make_state
from tests.unit._socket_closing_fixture import BODY_SENTINEL, socket_closing_fixture_page
from tests.unit.copilot_test_helpers import skip_no_browser


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
    def test_registers_six_listeners(self) -> None:
        state = _make_state()
        raw = MagicMock()
        raw.on = MagicMock()
        _register_hooks_on_page(state, raw)
        assert raw.on.call_count == 6
        events = {c.args[0] for c in raw.on.call_args_list}
        assert events == {"console", "response", "requestfailed", "close", "dialog", "pageerror"}

    def test_idempotent(self) -> None:
        state = _make_state()
        raw = MagicMock()
        raw.on = MagicMock()
        _register_hooks_on_page(state, raw)
        _register_hooks_on_page(state, raw)
        assert raw.on.call_count == 6

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
        assert raw1.on.call_count == 6
        assert raw2.on.call_count == 6


class TestRequestFailed:
    @skip_no_browser
    @pytest.mark.asyncio
    async def test_real_chromium_records_a_socket_closed_xhr_once_ignores_successes_and_forgets_a_closed_tab(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = RequestFailureLog()
        states = [_make_state(request_failures=log) for _ in range(3)]
        async with socket_closing_fixture_page() as (page, origin):
            tab_id = str(id(page))
            for state in states:
                state._codeblock_redactor = lambda value: json.loads(json.dumps(value).replace(tab_id, "[REDACTED]"))
                _register_hooks_on_page(state, page)
            await page.goto(f"{origin}/?token=page-secret")

            async with page.expect_response(f"{origin}/ok"):
                await page.click("#fast")
            async with page.expect_response(f"{origin}/slow", timeout=30_000):
                await page.click("#slow")
            assert log.entries == {}

            async with page.expect_event("requestfailed"):
                await page.click("#view")
            recorded = logged_request_failures(log)
            await page.close()
            assert log.entries == {}

        (entry,) = recorded
        assert set(entry) == {"url", "method", "resource_type", "failure_text", "page_url", "tab_id", "timestamp"}
        assert entry["method"] == "POST" and entry["resource_type"] == "xhr"
        assert entry["failure_text"] == "net::ERR_EMPTY_RESPONSE"
        assert entry["url"].startswith(f"{origin}/report?")
        assert "token=REDACTED" in entry["url"] and "request-secret" not in entry["url"]
        assert entry["page_url"] == f"{origin}/?token=REDACTED"
        assert entry["tab_id"] == tab_id
        assert BODY_SENTINEL not in json.dumps(entry)

        _patch(monkeypatch, states[0])
        network = await skyvern_network_requests(verbosity="full")
        requests = network["data"]["requests"]
        assert sorted((r["method"], r["url"], r["status"]) for r in requests) == [
            ("GET", f"{origin}/?token=REDACTED", 200),
            ("GET", f"{origin}/ok", 200),
            ("GET", f"{origin}/slow", 200),
        ]
        assert BODY_SENTINEL not in json.dumps(network)


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
