"""Tests for MCP tab management tools."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core import session_manager
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
    """Create a SessionState retained by a caller, as the stdio/global and copilot paths do."""
    state = SessionState()
    state.browser = browser
    state.tab_state_persists = True
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
async def test_tab_list_answers_within_its_bound_when_a_page_title_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_returns() -> str:
        await asyncio.sleep(3600)
        return "Slow"

    hanging = _make_mock_page("https://slow.com", "Slow")
    hanging.title = _never_returns
    browser = _make_mock_browser(hanging)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=hanging), ctx)))
    _patch_session(monkeypatch, _make_session_state(browser))
    monkeypatch.setattr(mcp_tabs, "TAB_TITLE_TIMEOUT_SECONDS", 0.05)

    result = await asyncio.wait_for(mcp_tabs.skyvern_tab_list(), timeout=5)

    assert result["ok"] is True
    assert result["data"]["tabs"][0]["title"] == ""
    assert result["data"]["tabs"][0]["url"] == "https://slow.com"


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

    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=False)
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=existing_page), ctx)))

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_new(url="https://target.com")

    assert result["ok"] is True
    new_page.goto.assert_awaited_once_with("https://target.com/", wait_until="domcontentloaded", timeout=30000)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "can_access_localhost"),
    [
        pytest.param("file:///etc/passwd", False, id="file"),
        pytest.param("http://169.254.169.254/", False, id="metadata"),
        pytest.param("http://10.20.30.40/", False, id="private"),
        pytest.param("http://127.0.0.1:8000/", False, id="loopback"),
        pytest.param("http://127.0.0.2/", False, id="alternate-loopback"),
        pytest.param("http://2130706433/", False, id="integer-loopback"),
        pytest.param("http://169.254.169.254/", True, id="metadata-local-context"),
        pytest.param("http://10.20.30.40/", True, id="private-local-context"),
    ],
)
async def test_tab_new_rejects_unsafe_url_before_opening(
    monkeypatch: pytest.MonkeyPatch, url: str, can_access_localhost: bool
) -> None:
    existing_page = _make_mock_page()
    new_page = _make_mock_page()
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    ctx = BrowserContext(
        mode="local" if can_access_localhost else "cloud_session",
        session_id=None if can_access_localhost else "pbs_test",
        can_access_localhost=can_access_localhost,
    )
    _patch_get_page(monkeypatch, existing_page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_tabs.skyvern_tab_new(url=url)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    browser._browser_context.new_page.assert_not_awaited()
    new_page.goto.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://localhost:3000/", "http://127.0.0.1:8000/"])
async def test_tab_new_allows_local_url_when_context_permits(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    existing_page = _make_mock_page()
    new_page = _make_mock_page(url, "Local")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    browser._browser_context.pages = [existing_page, new_page]
    ctx = BrowserContext(mode="local", can_access_localhost=True)
    _patch_get_page(monkeypatch, existing_page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_tabs.skyvern_tab_new(url=url)

    assert result["ok"] is True
    new_page.goto.assert_awaited_once_with(url, wait_until="domcontentloaded", timeout=30000)


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

    result = await mcp_tabs.skyvern_tab_new(url="https://example.com")

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

    active_before = state._active_page
    result = await mcp_tabs.skyvern_tab_switch(tab_id="nonexistent")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert state._active_page is active_before


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
        # 4 events per page: console, response, dialog, pageerror
        assert page_a.on.call_count == 4
        assert page_b.on.call_count == 4

    def test_hooks_idempotent(self) -> None:
        from skyvern.cli.mcp_tools.inspection import ensure_hooks_on_all_pages

        page_a = MagicMock()
        page_a.is_closed.return_value = False
        page_a.on = MagicMock()

        state = _make_session_state()

        ensure_hooks_on_all_pages(state, [page_a])
        ensure_hooks_on_all_pages(state, [page_a])

        # Should only register once
        assert page_a.on.call_count == 4

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
# Session-ownership guards
# ═══════════════════════════════════════════════════


class TestTabStatePersistenceGuards:
    """Tab tools that mutate session state refuse a caller whose state dies with the call."""

    @pytest.mark.asyncio
    async def test_tab_switch_rejects_per_request_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session_manager.set_stateless_http_mode(True)
        _patch_get_page(monkeypatch, _make_mock_page(), BrowserContext(mode="cloud_session", session_id="pbs_req"))
        result = await mcp_tabs.skyvern_tab_switch(tab_id="123")
        assert result["ok"] is False
        assert result["error"]["code"] == "ACTION_FAILED"

    @pytest.mark.asyncio
    async def test_tab_close_rejects_per_request_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session_manager.set_stateless_http_mode(True)
        _patch_get_page(monkeypatch, _make_mock_page(), BrowserContext(mode="cloud_session", session_id="pbs_req"))
        result = await mcp_tabs.skyvern_tab_close()
        assert result["ok"] is False
        assert result["error"]["code"] == "ACTION_FAILED"

    @pytest.mark.asyncio
    async def test_tab_wait_for_new_rejects_per_request_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session_manager.set_stateless_http_mode(True)
        _patch_get_page(monkeypatch, _make_mock_page(), BrowserContext(mode="cloud_session", session_id="pbs_req"))
        result = await mcp_tabs.skyvern_tab_wait_for_new()
        assert result["ok"] is False
        assert result["error"]["code"] == "ACTION_FAILED"

    @pytest.mark.asyncio
    async def test_tab_switch_allows_registered_copilot_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page_a = _make_mock_page("https://a.com", "A")
        page_b = _make_mock_page("https://b.com", "B")
        owned = SessionState(browser=_make_mock_browser(page_a, page_b), organization_id="org_tabs")
        session_manager.set_stateless_http_mode(True)
        session_manager.register_copilot_session("pbs_tabs", owned, organization_id="org_tabs")
        ctx = BrowserContext(mode="cloud_session", session_id="pbs_tabs")

        async def _get_page_installing_registered(**_kwargs: object) -> tuple[SimpleNamespace, BrowserContext]:
            session_manager.set_current_session(owned)
            return SimpleNamespace(page=page_a), ctx

        monkeypatch.setattr(mcp_tabs, "get_page", _get_page_installing_registered)
        try:
            result = await mcp_tabs.skyvern_tab_switch(tab_id=str(id(page_b)))
        finally:
            session_manager.unregister_copilot_session("pbs_tabs", organization_id="org_tabs")

        assert result["ok"] is True
        assert owned.tab_state_persists is True
        assert owned._active_page is page_b


# ═══════════════════════════════════════════════════
# skyvern_open_tabs
# ═══════════════════════════════════════════════════


def _patch_open_tabs(monkeypatch: pytest.MonkeyPatch) -> tuple[SessionState, MagicMock]:
    existing_page = _make_mock_page("https://old.com", "Old")
    opened_pages = [_make_mock_page("https://a.com", "A"), _make_mock_page("https://b.com", "B")]
    browser = _make_mock_browser(existing_page, *opened_pages)
    browser._browser_context.new_page = AsyncMock(side_effect=list(opened_pages))

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(SimpleNamespace(page=existing_page), ctx)))
    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)
    clear = MagicMock()
    monkeypatch.setattr(mcp_tabs, "clear_session_ref_map", clear)
    return state, clear


@pytest.mark.asyncio
async def test_open_tabs_rejects_invalid_wait_until_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page must not run"))
    monkeypatch.setattr(mcp_tabs, "get_page", get_page)

    result = await mcp_tabs.skyvern_open_tabs(urls=["https://example.com"], wait_until="networkidle0")

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_tabs_rejects_more_than_batch_limit_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    get_page = AsyncMock(side_effect=AssertionError("get_page must not run"))
    monkeypatch.setattr(mcp_tabs, "get_page", get_page)
    urls = [f"https://example.com/{index}" for index in range(41)]

    result = await mcp_tabs.skyvern_open_tabs(urls=urls, screenshot=False)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert "40" in result["error"]["message"]
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_tabs_rejects_localhost_before_creating_page(monkeypatch: pytest.MonkeyPatch) -> None:
    existing_page = _make_mock_page("https://old.example", "Old")
    browser = _make_mock_browser(existing_page)
    context = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=False)
    monkeypatch.setattr(
        mcp_tabs,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=existing_page), context)),
    )
    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_open_tabs(urls=["http://localhost:8080/private"], screenshot=False)

    assert result["ok"] is True
    assert result["data"]["opened"] == 0
    assert result["data"]["failed"] == 1
    assert "localhost" in result["data"]["tabs"][0]["error"].lower()
    browser._browser_context.new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_tabs_keeps_success_when_page_leaves_context_list(monkeypatch: pytest.MonkeyPatch) -> None:
    existing_page = _make_mock_page("https://old.example", "Old")
    self_closing_page = _make_mock_page("https://done.example", "Done")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=self_closing_page)
    context = BrowserContext(mode="local")
    monkeypatch.setattr(
        mcp_tabs,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=existing_page), context)),
    )
    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_open_tabs(urls=["https://done.example"], screenshot=False)

    assert result["ok"] is True
    assert result["data"]["opened"] == 1
    assert result["data"]["failed"] == 0
    assert result["data"]["tabs"][0]["index"] == 0
    self_closing_page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_tabs_keeping_the_active_tab_preserves_the_ref_map(monkeypatch: pytest.MonkeyPatch) -> None:
    # The observed page is unchanged, so refs from a prior observe are still resolvable — clearing
    # them here would cost a re-observe for nothing.
    state, clear = _patch_open_tabs(monkeypatch)

    result = await mcp_tabs.skyvern_open_tabs(urls=["https://a.com", "https://b.com"], screenshot=False)

    assert result["ok"] is True, result
    assert result["data"]["opened"] == 2
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_open_tabs_clears_the_ref_map_when_it_moves_the_active_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    # Parity with tab_new / tab_switch / tab_close: refs captured on the previous tab must not
    # resolve against the newly active one.
    state, clear = _patch_open_tabs(monkeypatch)

    result = await mcp_tabs.skyvern_open_tabs(
        urls=["https://a.com", "https://b.com"], screenshot=False, set_active_last=True
    )

    assert result["ok"] is True, result
    assert state._active_page is not None
    assert state._active_page.url == "https://b.com"
    clear.assert_called_once()


@pytest.mark.asyncio
async def test_open_tabs_closes_page_when_navigation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    existing_page = _make_mock_page("https://old.com", "Old")
    failed_page = _make_mock_page("https://failed.example", "Failed")
    failed_page.goto.side_effect = RuntimeError("navigation failed")
    browser = _make_mock_browser(existing_page, failed_page)
    browser._browser_context.new_page = AsyncMock(return_value=failed_page)
    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(
        mcp_tabs,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=existing_page), ctx)),
    )
    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_open_tabs(urls=["https://failed.example"], screenshot=False)

    assert result["ok"] is True
    assert result["data"]["opened"] == 0
    assert result["data"]["failed"] == 1
    failed_page.close.assert_awaited_once()
    assert state._active_page is existing_page


@pytest.mark.asyncio
async def test_open_tabs_does_not_screenshot_preexisting_tab_when_every_navigation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_page = _make_mock_page("https://old.example", "Old")
    failed_page = _make_mock_page("https://failed.example", "Failed")
    failed_page.goto.side_effect = RuntimeError("navigation failed")
    browser = _make_mock_browser(existing_page, failed_page)
    browser._browser_context.new_page = AsyncMock(return_value=failed_page)
    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(
        mcp_tabs,
        "get_page",
        AsyncMock(return_value=(SimpleNamespace(page=existing_page), ctx)),
    )
    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)
    screenshot = AsyncMock()
    monkeypatch.setattr(mcp_tabs, "do_screenshot", screenshot)

    result = await mcp_tabs.skyvern_open_tabs(urls=["https://failed.example"], screenshot=True)

    assert result["ok"] is True
    assert result["data"]["path"] is None
    assert result["artifacts"] == []
    screenshot.assert_not_awaited()
