"""Tests for MCP HAR recording tools (skyvern_har_start, skyvern_har_stop)."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import inspection as mcp_inspection

# ═══════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════


def _make_mock_page(url: str = "https://example.com") -> MagicMock:
    page = MagicMock()
    page.url = url
    return page


def _make_skyvern_page(page: MagicMock) -> MagicMock:
    wrapper = MagicMock()
    wrapper.page = page
    wrapper.url = page.url
    return wrapper


def _make_session_state(**overrides):
    defaults = {
        "har_enabled": False,
        "_har_entries": deque(maxlen=5000),
        "console_messages": deque(maxlen=1000),
        "network_requests": deque(maxlen=1000),
        "dialog_events": deque(maxlen=1000),
        "_hooked_page_ids": set(),
        "_hooked_handlers_map": {},
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch_get_page(monkeypatch: pytest.MonkeyPatch, page: MagicMock, ctx: BrowserContext) -> AsyncMock:
    skyvern_page = _make_skyvern_page(page)
    mock = AsyncMock(return_value=(skyvern_page, ctx))
    monkeypatch.setattr(mcp_inspection, "get_page", mock)
    return mock


def _patch_stateless(monkeypatch: pytest.MonkeyPatch, stateless: bool = False) -> None:
    monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: stateless)


# ═══════════════════════════════════════════════════
# HAR entry capture in _on_response
# ═══════════════════════════════════════════════════


def test_on_response_captures_har_when_enabled() -> None:
    state = _make_session_state(har_enabled=True)
    raw_page = MagicMock()
    raw_page.url = "https://example.com"

    handlers = mcp_inspection._make_page_handlers(state, raw_page)
    on_response = handlers["response"]

    response = MagicMock()
    response.url = "https://api.example.com/data"
    response.status = 200
    response.status_text = "OK"
    response.headers = {"content-type": "application/json", "content-length": "42"}
    response.request.method = "GET"
    response.request.headers = {"accept": "application/json"}
    response.request.timing = {"responseEnd": 150.5}

    on_response(response)

    assert len(state._har_entries) == 1
    entry = state._har_entries[0]
    assert entry["request"]["method"] == "GET"
    assert entry["request"]["httpVersion"] == "HTTP/1.1"
    assert entry["request"]["queryString"] == []
    assert entry["request"]["cookies"] == []
    assert entry["request"]["headersSize"] == -1
    assert entry["request"]["bodySize"] == -1
    assert entry["response"]["status"] == 200
    assert entry["response"]["httpVersion"] == "HTTP/1.1"
    assert entry["response"]["redirectURL"] == ""
    assert entry["response"]["headersSize"] == -1
    assert entry["response"]["bodySize"] == -1
    assert entry["response"]["cookies"] == []
    assert entry["response"]["content"]["mimeType"] == "application/json"
    assert entry["response"]["content"]["size"] == 42


def test_on_response_skips_har_when_disabled() -> None:
    state = _make_session_state(har_enabled=False)
    raw_page = MagicMock()
    raw_page.url = "https://example.com"

    handlers = mcp_inspection._make_page_handlers(state, raw_page)
    on_response = handlers["response"]

    response = MagicMock()
    response.url = "https://api.example.com/data"
    response.status = 200
    response.headers = {"content-type": "text/html"}
    response.request.method = "GET"
    response.request.headers = {}
    response.request.timing = {}

    on_response(response)

    assert len(state._har_entries) == 0
    assert len(state.network_requests) == 1  # Normal capture still works


def test_on_response_redacts_auth_headers_in_har() -> None:
    state = _make_session_state(har_enabled=True)
    raw_page = MagicMock()
    raw_page.url = "https://example.com"

    handlers = mcp_inspection._make_page_handlers(state, raw_page)
    on_response = handlers["response"]

    response = MagicMock()
    response.url = "https://api.example.com/data"
    response.status = 200
    response.status_text = "OK"
    response.headers = {"content-type": "text/html", "set-cookie": "session=abc123"}
    response.request.method = "GET"
    response.request.headers = {"authorization": "Bearer token123", "accept": "text/html", "cookie": "session=old"}
    response.request.timing = {}

    on_response(response)

    entry = state._har_entries[0]
    req_header_names = [h["name"] for h in entry["request"]["headers"]]
    assert "authorization" not in req_header_names
    assert "cookie" not in req_header_names
    assert "accept" in req_header_names

    resp_header_names = [h["name"] for h in entry["response"]["headers"]]
    assert "set-cookie" not in resp_header_names
    assert "content-type" in resp_header_names


def test_on_response_redacts_secret_query_params_in_har() -> None:
    state = _make_session_state(har_enabled=True)
    raw_page = MagicMock()
    raw_page.url = "https://example.com"

    handlers = mcp_inspection._make_page_handlers(state, raw_page)
    on_response = handlers["response"]

    response = MagicMock()
    response.url = "https://api.example.com/data?token=secret123&foo=bar&api_key=hidden"
    response.status = 200
    response.status_text = "OK"
    response.headers = {"content-type": "text/html"}
    response.request.method = "GET"
    response.request.headers = {"accept": "text/html"}
    response.request.timing = {}

    on_response(response)

    entry = state._har_entries[0]
    qs = {p["name"]: p["value"] for p in entry["request"]["queryString"]}
    assert qs["foo"] == "bar"
    assert qs["token"] == "REDACTED"
    assert qs["api_key"] == "REDACTED"


# ═══════════════════════════════════════════════════
# skyvern_har_start
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_har_start_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_stateless(monkeypatch, False)

    state = _make_session_state()
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)

    result = await mcp_inspection.skyvern_har_start()

    assert result["ok"] is True
    assert state.har_enabled is True
    assert result["data"]["recording"] is True


@pytest.mark.asyncio
async def test_har_start_already_active(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_stateless(monkeypatch, False)

    state = _make_session_state(har_enabled=True)
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)

    result = await mcp_inspection.skyvern_har_start()

    assert result["ok"] is False
    assert "already active" in result["error"]["message"]


@pytest.mark.asyncio
async def test_har_start_clears_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_stateless(monkeypatch, False)

    entries = deque(maxlen=5000)
    entries.append({"old": "entry"})
    state = _make_session_state(_har_entries=entries)
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)

    result = await mcp_inspection.skyvern_har_start()

    assert result["ok"] is True
    assert len(state._har_entries) == 0


@pytest.mark.asyncio
async def test_har_start_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_inspection, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    _patch_stateless(monkeypatch, False)

    result = await mcp_inspection.skyvern_har_start()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_har_start_stateless_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stateless(monkeypatch, True)

    result = await mcp_inspection.skyvern_har_start()
    assert result["ok"] is False
    assert "stateless" in result["error"]["message"].lower()


# ═══════════════════════════════════════════════════
# skyvern_har_stop
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_har_stop_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_stateless(monkeypatch, False)

    entries = deque(maxlen=5000)
    entries.append(
        {
            "startedDateTime": "2026-01-01T00:00:00Z",
            "time": 100,
            "request": {"method": "GET", "url": "https://example.com", "headers": []},
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [],
                "content": {"size": 1024, "mimeType": "text/html"},
            },
            "timings": {"send": 0, "wait": 100, "receive": 0},
        }
    )
    state = _make_session_state(har_enabled=True, _har_entries=entries)
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)

    result = await mcp_inspection.skyvern_har_stop()

    assert result["ok"] is True
    assert state.har_enabled is False
    assert len(state._har_entries) == 0
    assert result["data"]["entry_count"] == 1
    har = result["data"]["har"]
    assert har["log"]["version"] == "1.2"
    assert har["log"]["creator"]["name"] == "Skyvern"
    assert len(har["log"]["entries"]) == 1


@pytest.mark.asyncio
async def test_har_stop_not_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_stateless(monkeypatch, False)

    state = _make_session_state(har_enabled=False)
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)

    result = await mcp_inspection.skyvern_har_stop()

    assert result["ok"] is False
    assert "No active HAR recording" in result["error"]["message"]


@pytest.mark.asyncio
async def test_har_stop_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_inspection, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    _patch_stateless(monkeypatch, False)

    result = await mcp_inspection.skyvern_har_stop()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_har_stop_stateless_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stateless(monkeypatch, True)

    result = await mcp_inspection.skyvern_har_stop()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_har_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start → capture entries → stop → verify HAR output."""
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_stateless(monkeypatch, False)

    state = _make_session_state()
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)

    # Start
    result = await mcp_inspection.skyvern_har_start()
    assert result["ok"] is True
    assert state.har_enabled is True

    # Simulate entries being added (as _on_response would do)
    state._har_entries.append(
        {
            "startedDateTime": "2026-01-01T00:00:00Z",
            "time": 50,
            "request": {"method": "POST", "url": "https://api.example.com/submit", "headers": []},
            "response": {"status": 201, "statusText": "Created", "headers": [], "content": {"size": 0, "mimeType": ""}},
            "timings": {"send": 0, "wait": 50, "receive": 0},
        }
    )

    # Stop
    result = await mcp_inspection.skyvern_har_stop()
    assert result["ok"] is True
    assert result["data"]["entry_count"] == 1
    assert state.har_enabled is False
    assert len(state._har_entries) == 0
