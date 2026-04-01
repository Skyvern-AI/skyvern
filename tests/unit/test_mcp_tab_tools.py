"""Tests for MCP tab management tools."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState
from skyvern.cli.mcp_tools import tabs as mcp_tabs


def _make_mock_page(url: str = "https://example.com", title: str = "Example", *, closed: bool = False) -> MagicMock:
    """Create a mock Playwright Page with common attributes."""
    page = MagicMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.is_closed.return_value = closed
    page.close = AsyncMock()
    page.bring_to_front = AsyncMock()
    page.goto = AsyncMock()
    return page


def _make_mock_browser(*pages: MagicMock) -> MagicMock:
    """Create a mock SkyvernBrowser with given pages."""
    browser = MagicMock()
    browser._browser_context = MagicMock()
    browser._browser_context.pages = list(pages)
    browser._browser_context.new_page = AsyncMock()
    browser._browser_context.on = MagicMock()
    return browser


def _make_session_state(browser: MagicMock | None = None) -> SessionState:
    """Create a SessionState with tab management fields."""
    state = SessionState()
    state.browser = browser
    return state


def _patch_get_page(monkeypatch: pytest.MonkeyPatch, page: MagicMock, ctx: BrowserContext) -> AsyncMock:
    """Patch get_page to return a SkyvernBrowserPage-like wrapper."""
    skyvern_page = SimpleNamespace(page=page)
    mock = AsyncMock(return_value=(skyvern_page, ctx))
    monkeypatch.setattr(mcp_tabs, "get_page", mock)
    return mock


def _patch_session(monkeypatch: pytest.MonkeyPatch, state: SessionState) -> MagicMock:
    mock = MagicMock(return_value=state)
    monkeypatch.setattr(mcp_tabs, "get_current_session", mock)
    return mock


# ═══════════════════════════════════════════════════
# skyvern_tab_list
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tab_list_returns_all_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "Page A")
    page_b = _make_mock_page("https://b.com", "Page B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    skyvern_page = SimpleNamespace(page=page_a)
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(skyvern_page, ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_list()

    assert result["ok"] is True
    tabs = result["data"]["tabs"]
    assert len(tabs) == 2
    assert tabs[0]["url"] == "https://a.com"
    assert tabs[0]["is_active"] is True
    assert tabs[1]["url"] == "https://b.com"
    assert tabs[1]["is_active"] is False
    assert result["data"]["count"] == 2


@pytest.mark.asyncio
async def test_tab_list_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(side_effect=mcp_tabs.BrowserNotAvailableError()))

    result = await mcp_tabs.skyvern_tab_list()

    assert result["ok"] is False
    assert result["error"]["code"] == "NO_ACTIVE_BROWSER"


# ═══════════════════════════════════════════════════
# skyvern_tab_new
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tab_new_creates_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    existing_page = _make_mock_page("https://old.com", "Old")
    new_page = _make_mock_page("about:blank", "New Tab")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)

    ctx = BrowserContext(mode="local")
    skyvern_page = SimpleNamespace(page=existing_page)
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(skyvern_page, ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    # After new_page(), browser.pages should include both
    browser._browser_context.pages = [existing_page, new_page]

    result = await mcp_tabs.skyvern_tab_new()

    assert result["ok"] is True
    assert result["data"]["is_active"] is True
    assert state._active_page is new_page
    browser._browser_context.new_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_tab_new_with_url(monkeypatch: pytest.MonkeyPatch) -> None:
    existing_page = _make_mock_page()
    new_page = _make_mock_page("https://target.com", "Target")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    browser._browser_context.pages = [existing_page, new_page]

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=existing_page), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_new(url="https://target.com")

    assert result["ok"] is True
    new_page.goto.assert_awaited_once_with("https://target.com", wait_until="domcontentloaded", timeout=30000)


@pytest.mark.asyncio
async def test_tab_new_navigation_failure_restores_previous_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """When goto() fails, active page should revert to the previous tab, not None."""
    existing_page = _make_mock_page("https://old.com", "Old")
    new_page = _make_mock_page("about:blank", "New Tab")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    browser._browser_context.pages = [existing_page, new_page]

    new_page.goto = AsyncMock(side_effect=Exception("Navigation failed"))
    new_page.close = AsyncMock()

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=existing_page), ctx)))

    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_new(url="https://bad-url.com")

    assert result["ok"] is False
    # Previous active page should be restored, not reset to None
    assert state._active_page is existing_page
    new_page.close.assert_awaited_once()


# ═══════════════════════════════════════════════════
# skyvern_tab_switch
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tab_switch_by_tab_id(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    target_id = str(id(page_b))
    result = await mcp_tabs.skyvern_tab_switch(tab_id=target_id)

    assert result["ok"] is True
    assert result["data"]["tab_id"] == target_id
    assert result["data"]["is_active"] is True
    assert state._active_page is page_b


@pytest.mark.asyncio
async def test_tab_switch_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_switch(index=1)

    assert result["ok"] is True
    assert state._active_page is page_b


@pytest.mark.asyncio
async def test_tab_switch_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preflight: must provide tab_id or index."""
    get_page = AsyncMock(side_effect=AssertionError("should not be called"))
    monkeypatch.setattr(mcp_tabs, "get_page", get_page)

    result = await mcp_tabs.skyvern_tab_switch()

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_tab_switch_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page()
    browser = _make_mock_browser(page_a)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_switch(tab_id="nonexistent")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


# ═══════════════════════════════════════════════════
# skyvern_tab_close
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tab_close_active_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    state._active_page = page_a
    _patch_session(monkeypatch, state)

    # After close, only page_b remains
    def _close_side_effect() -> None:
        browser._browser_context.pages = [page_b]

    page_a.close = AsyncMock(side_effect=_close_side_effect)

    result = await mcp_tabs.skyvern_tab_close()

    assert result["ok"] is True
    assert result["data"]["closed_tab_id"] == str(id(page_a))
    assert result["data"]["remaining_tabs"] == 1
    assert state._active_page is None
    page_a.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_tab_close_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    def _close_side_effect() -> None:
        browser._browser_context.pages = [page_a]

    page_b.close = AsyncMock(side_effect=_close_side_effect)

    result = await mcp_tabs.skyvern_tab_close(index=1)

    assert result["ok"] is True
    assert result["data"]["closed_tab_id"] == str(id(page_b))
    assert result["data"]["remaining_tabs"] == 1


@pytest.mark.asyncio
async def test_tab_close_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page()
    browser = _make_mock_browser(page_a)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_close(tab_id="nonexistent")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


# ═══════════════════════════════════════════════════
# skyvern_tab_wait_for_new
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tab_wait_for_new_from_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a page event is already buffered, return immediately."""
    page_a = _make_mock_page("https://a.com", "A")
    popup = _make_mock_page("https://popup.com", "Popup")
    browser = _make_mock_browser(page_a, popup)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    state._page_events.append(
        {"tab_id": str(id(popup)), "url": "https://popup.com", "timestamp": time.time(), "page": popup}
    )
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_wait_for_new()

    assert result["ok"] is True
    assert result["data"]["url"] == "https://popup.com"
    assert result["data"]["is_active"] is False  # Does NOT auto-switch


@pytest.mark.asyncio
async def test_tab_wait_for_new_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    browser = _make_mock_browser(page_a)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_wait_for_new(timeout_ms=1000)

    assert result["ok"] is False
    assert result["error"]["code"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_tab_wait_for_new_arrives_async(monkeypatch: pytest.MonkeyPatch) -> None:
    """Page event arrives after we start waiting."""
    page_a = _make_mock_page("https://a.com", "A")
    popup = _make_mock_page("https://popup.com", "Popup")
    browser = _make_mock_browser(page_a)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=page_a), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    async def _simulate_popup() -> None:
        await asyncio.sleep(0.2)
        browser._browser_context.pages = [page_a, popup]
        state._page_events.append(
            {"tab_id": str(id(popup)), "url": "https://popup.com", "timestamp": time.time(), "page": popup}
        )
        state._page_event_signal.set()

    asyncio.create_task(_simulate_popup())

    result = await mcp_tabs.skyvern_tab_wait_for_new(timeout_ms=5000)

    assert result["ok"] is True
    assert result["data"]["url"] == "https://popup.com"


# ═══════════════════════════════════════════════════
# Multi-page inspection hooks
# ═══════════════════════════════════════════════════


class TestMultiPageInspectionHooks:
    def test_hooks_registered_on_all_pages(self) -> None:
        from skyvern.cli.mcp_tools.inspection import ensure_hooks_on_all_pages

        page_a = MagicMock()
        page_a.is_closed.return_value = False
        page_a.on = MagicMock()

        page_b = MagicMock()
        page_b.is_closed.return_value = False
        page_b.on = MagicMock()

        state = _make_session_state()

        ensure_hooks_on_all_pages(state, [page_a, page_b])

        # Both pages should have hooks
        assert id(page_a) in state._hooked_page_ids
        assert id(page_b) in state._hooked_page_ids
        # 3 events per page: console, response, dialog
        assert page_a.on.call_count == 3
        assert page_b.on.call_count == 3

    def test_hooks_idempotent(self) -> None:
        from skyvern.cli.mcp_tools.inspection import ensure_hooks_on_all_pages

        page_a = MagicMock()
        page_a.is_closed.return_value = False
        page_a.on = MagicMock()

        state = _make_session_state()

        ensure_hooks_on_all_pages(state, [page_a])
        ensure_hooks_on_all_pages(state, [page_a])

        # Should only register once
        assert page_a.on.call_count == 3

    def test_stale_pages_pruned(self) -> None:
        from skyvern.cli.mcp_tools.inspection import ensure_hooks_on_all_pages

        page_a = MagicMock()
        page_a.is_closed.return_value = False
        page_a.on = MagicMock()

        page_b = MagicMock()
        page_b.is_closed.return_value = False
        page_b.on = MagicMock()

        state = _make_session_state()

        # Register both
        ensure_hooks_on_all_pages(state, [page_a, page_b])
        assert len(state._hooked_page_ids) == 2

        # page_b removed from context (closed)
        ensure_hooks_on_all_pages(state, [page_a])
        assert id(page_b) not in state._hooked_page_ids
        assert id(page_a) in state._hooked_page_ids


# ═══════════════════════════════════════════════════
# SessionState active page tracking
# ═══════════════════════════════════════════════════


class TestActivePageTracking:
    def test_active_page_default_none(self) -> None:
        state = SessionState()
        assert state._active_page is None

    def test_page_events_buffer(self) -> None:
        state = SessionState()
        assert len(state._page_events) == 0
        state._page_events.append({"test": True})
        assert len(state._page_events) == 1

    def test_hooked_page_ids_default_empty(self) -> None:
        state = SessionState()
        assert len(state._hooked_page_ids) == 0
        assert len(state._hooked_handlers_map) == 0


# ═══════════════════════════════════════════════════
# Tab resolution helper
# ═══════════════════════════════════════════════════


class TestResolveTab:
    def test_resolve_by_tab_id(self) -> None:
        page_a = _make_mock_page()
        page_b = _make_mock_page()
        pages = [page_a, page_b]

        result = mcp_tabs._resolve_tab(pages, tab_id=str(id(page_b)))
        assert result is page_b

    def test_resolve_by_index(self) -> None:
        page_a = _make_mock_page()
        page_b = _make_mock_page()
        pages = [page_a, page_b]

        assert mcp_tabs._resolve_tab(pages, index=0) is page_a
        assert mcp_tabs._resolve_tab(pages, index=1) is page_b

    def test_resolve_out_of_range(self) -> None:
        page_a = _make_mock_page()
        assert mcp_tabs._resolve_tab([page_a], index=5) is None

    def test_resolve_not_found(self) -> None:
        page_a = _make_mock_page()
        assert mcp_tabs._resolve_tab([page_a], tab_id="nonexistent") is None

    def test_resolve_no_args(self) -> None:
        assert mcp_tabs._resolve_tab([]) is None

    def test_resolve_skips_closed_page_by_id(self) -> None:
        page = _make_mock_page(closed=True)
        assert mcp_tabs._resolve_tab([page], tab_id=str(id(page))) is None

    def test_resolve_skips_closed_page_by_index(self) -> None:
        page = _make_mock_page(closed=True)
        assert mcp_tabs._resolve_tab([page], index=0) is None


# ═══════════════════════════════════════════════════
# Stateless HTTP mode guards
# ═══════════════════════════════════════════════════


class TestStatelessModeGuards:
    """Tab tools that rely on session state must reject stateless HTTP mode."""

    @pytest.mark.asyncio
    async def test_tab_switch_rejects_stateless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: True)
        result = await mcp_tabs.skyvern_tab_switch(tab_id="123")
        assert result["ok"] is False
        assert result["error"]["code"] == "ACTION_FAILED"

    @pytest.mark.asyncio
    async def test_tab_close_rejects_stateless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: True)
        result = await mcp_tabs.skyvern_tab_close()
        assert result["ok"] is False
        assert result["error"]["code"] == "ACTION_FAILED"

    @pytest.mark.asyncio
    async def test_tab_wait_for_new_rejects_stateless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: True)
        result = await mcp_tabs.skyvern_tab_wait_for_new()
        assert result["ok"] is False
        assert result["error"]["code"] == "ACTION_FAILED"
