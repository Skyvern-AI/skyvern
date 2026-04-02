"""Tests for MCP page JS error tool (skyvern_get_errors) and pageerror hook."""

from __future__ import annotations

import time
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
        "page_errors": deque(maxlen=1000),
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


# ═══════════════════════════════════════════════════
# pageerror hook registration
# ═══════════════════════════════════════════════════


def test_make_page_handlers_includes_pageerror() -> None:
    state = _make_session_state()
    raw_page = MagicMock()
    raw_page.url = "https://example.com"

    handlers = mcp_inspection._make_page_handlers(state, raw_page)

    assert "pageerror" in handlers


def test_pageerror_handler_appends_to_buffer() -> None:
    state = _make_session_state()
    raw_page = MagicMock()
    raw_page.url = "https://example.com"

    handlers = mcp_inspection._make_page_handlers(state, raw_page)
    handler = handlers["pageerror"]

    handler(Exception("ReferenceError: foo is not defined"))

    assert len(state.page_errors) == 1
    entry = state.page_errors[0]
    assert "ReferenceError" in entry["message"]
    assert entry["page_url"] == "https://example.com"
    assert "timestamp" in entry
    assert "tab_id" in entry


def test_pageerror_handler_survives_exception() -> None:
    """Handler should never crash even with bizarre error objects."""
    state = _make_session_state()
    raw_page = MagicMock()
    raw_page.url = "https://example.com"

    handlers = mcp_inspection._make_page_handlers(state, raw_page)
    handler = handlers["pageerror"]

    # Calling with an object whose str() raises shouldn't crash
    class BadError:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    handler(BadError())
    # Should not raise — errors are silently caught
    assert len(state.page_errors) == 1


def test_register_hooks_registers_pageerror() -> None:
    state = _make_session_state()
    raw_page = MagicMock()
    raw_page.url = "https://example.com"

    mcp_inspection._register_hooks_on_page(state, raw_page)

    # Check that pageerror was registered
    on_calls = [call for call in raw_page.on.call_args_list if call[0][0] == "pageerror"]
    assert len(on_calls) == 1


# ═══════════════════════════════════════════════════
# skyvern_get_errors — happy path
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_errors_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    state = _make_session_state()
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)
    monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: False)

    result = await mcp_inspection.skyvern_get_errors()

    assert result["ok"] is True
    assert result["data"]["count"] == 0
    assert result["data"]["errors"] == []


@pytest.mark.asyncio
async def test_get_errors_returns_buffered_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    errors = deque(maxlen=1000)
    errors.append(
        {
            "message": "TypeError: null is not an object",
            "timestamp": time.time(),
            "page_url": "https://example.com",
            "tab_id": "1",
        }
    )
    errors.append(
        {
            "message": "ReferenceError: x is not defined",
            "timestamp": time.time(),
            "page_url": "https://example.com",
            "tab_id": "1",
        }
    )
    state = _make_session_state(page_errors=errors)
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)
    monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: False)

    result = await mcp_inspection.skyvern_get_errors()

    assert result["ok"] is True
    assert result["data"]["count"] == 2


@pytest.mark.asyncio
async def test_get_errors_text_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    errors = deque(maxlen=1000)
    errors.append(
        {
            "message": "TypeError: null is not an object",
            "timestamp": time.time(),
            "page_url": "https://example.com",
            "tab_id": "1",
        }
    )
    errors.append(
        {
            "message": "ReferenceError: x is not defined",
            "timestamp": time.time(),
            "page_url": "https://example.com",
            "tab_id": "1",
        }
    )
    state = _make_session_state(page_errors=errors)
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)
    monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: False)

    result = await mcp_inspection.skyvern_get_errors(text="TypeError")

    assert result["ok"] is True
    assert result["data"]["count"] == 1
    assert "TypeError" in result["data"]["errors"][0]["message"]


@pytest.mark.asyncio
async def test_get_errors_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    errors = deque(maxlen=1000)
    errors.append({"message": "Error 1", "timestamp": time.time(), "page_url": "https://example.com", "tab_id": "1"})
    errors.append({"message": "Error 2", "timestamp": time.time(), "page_url": "https://example.com", "tab_id": "1"})
    state = _make_session_state(page_errors=errors)
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)
    monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: False)

    result = await mcp_inspection.skyvern_get_errors(clear=True)

    assert result["ok"] is True
    assert result["data"]["count"] == 2
    assert len(state.page_errors) == 0


@pytest.mark.asyncio
async def test_get_errors_clear_with_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)

    errors = deque(maxlen=1000)
    errors.append(
        {"message": "TypeError: null", "timestamp": time.time(), "page_url": "https://example.com", "tab_id": "1"}
    )
    errors.append(
        {"message": "ReferenceError: x", "timestamp": time.time(), "page_url": "https://example.com", "tab_id": "1"}
    )
    state = _make_session_state(page_errors=errors)
    monkeypatch.setattr(mcp_inspection, "get_current_session", lambda: state)
    monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: False)

    result = await mcp_inspection.skyvern_get_errors(text="TypeError", clear=True)

    assert result["ok"] is True
    assert result["data"]["count"] == 1
    # Only the matched one was removed; the other remains
    assert len(state.page_errors) == 1
    assert "ReferenceError" in state.page_errors[0]["message"]


# ═══════════════════════════════════════════════════
# skyvern_get_errors — error cases
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_errors_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_inspection, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: False)

    result = await mcp_inspection.skyvern_get_errors()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_get_errors_stateless_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: True)

    result = await mcp_inspection.skyvern_get_errors()
    assert result["ok"] is False
    assert "stateless" in result["error"]["message"].lower()
