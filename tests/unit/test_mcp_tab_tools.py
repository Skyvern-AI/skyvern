"""Tests for MCP tab management tools."""

from __future__ import annotations

import asyncio
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core import session_manager
from skyvern.cli.core.browser_ops import NavigateResult
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState
from skyvern.cli.mcp_tools import tabs as mcp_tabs
from skyvern.webeye import action_deadline


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
    """Patch page and browser resolution for tests that use a retained session."""
    skyvern_page = SimpleNamespace(page=page)
    mock = AsyncMock(return_value=(skyvern_page, ctx))
    monkeypatch.setattr(mcp_tabs, "get_page", mock)

    async def _resolve_browser(**_kwargs: object) -> tuple[MagicMock, BrowserContext]:
        state = mcp_tabs.get_current_session()
        return state.browser or MagicMock(), ctx

    monkeypatch.setattr(mcp_tabs, "resolve_browser", _resolve_browser)
    return mock


def _patch_session(monkeypatch: pytest.MonkeyPatch, state: SessionState) -> MagicMock:
    mock = MagicMock(return_value=state)
    monkeypatch.setattr(mcp_tabs, "get_current_session", mock)
    return mock


@pytest.fixture
def public_dns_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_to_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_to_public)


# ═══════════════════════════════════════════════════
# skyvern_tab_list
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tab_list_returns_all_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "Page A")
    page_b = _make_mock_page("https://b.com", "Page B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page_a, ctx)

    state = _make_session_state(browser)
    state._active_page = page_a
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_list()

    assert result["ok"] is True
    tabs = result["data"]["tabs"]
    assert len(tabs) == 2
    assert tabs[0]["url"] == "https://a.com"
    assert tabs[0]["is_active"] is True
    assert tabs[1]["url"] == "https://b.com"
    assert tabs[1]["is_active"] is False
    assert "debugger_attached" not in tabs[0]
    assert result["data"]["count"] == 2


@pytest.mark.asyncio
async def test_tab_list_prefers_implicit_page_over_newer_popup(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    popup_b = _make_mock_page("https://b.com", "B")
    popup_c = _make_mock_page("https://c.com", "C")
    browser = _make_mock_browser(page_a, popup_b, popup_c)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page_a, ctx)
    state = _make_session_state(browser)
    state._active_page = None
    state._implicit_page = page_a
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_list()

    assert result["ok"] is True
    assert result["data"]["active_tab_id"] == str(id(page_a))
    assert [tab["is_active"] for tab in result["data"]["tabs"]] == [True, False, False]


@pytest.mark.asyncio
async def test_tab_list_falls_back_when_implicit_page_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    closed_page = _make_mock_page("https://closed.example", "Closed", closed=True)
    remaining_page = _make_mock_page("https://remaining.example", "Remaining")
    browser = _make_mock_browser(closed_page, remaining_page)

    ctx = BrowserContext(mode="local")
    monkeypatch.setattr(mcp_tabs, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
    monkeypatch.setattr(mcp_tabs, "ensure_browser_hooks", MagicMock())
    state = _make_session_state(browser)
    state._implicit_page = closed_page
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_list()

    assert result["ok"] is True
    assert result["data"]["active_tab_id"] == str(id(remaining_page))
    assert result["data"]["tabs"][0]["is_active"] is False
    assert result["data"]["tabs"][1]["is_active"] is True


@pytest.mark.asyncio
async def test_tab_list_pins_newest_page_when_no_selection_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    popup_c = _make_mock_page("https://c.com", "C")
    browser = _make_mock_browser(page_a, page_b)
    ctx = BrowserContext(mode="local")
    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)
    monkeypatch.setattr(mcp_tabs, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
    monkeypatch.setattr(mcp_tabs, "ensure_browser_hooks", MagicMock())

    listed = await mcp_tabs.skyvern_tab_list()

    assert listed["ok"] is True
    assert listed["data"]["active_tab_id"] == str(id(page_b))
    assert state._implicit_page is page_b

    browser._browser_context.pages.append(popup_c)
    page_b_wrapper = SimpleNamespace(page=page_b)
    browser.get_page_for = AsyncMock(return_value=page_b_wrapper)
    browser.get_working_page = AsyncMock(return_value=SimpleNamespace(page=popup_c))
    monkeypatch.setattr(session_manager, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
    monkeypatch.setattr(session_manager, "get_current_session", lambda: state)
    monkeypatch.setattr(session_manager, "ensure_browser_hooks", MagicMock())

    page, _ = await session_manager.get_page()

    assert page is page_b_wrapper
    browser.get_page_for.assert_awaited_once_with(page_b)
    browser.get_working_page.assert_not_awaited()

    listed_again = await mcp_tabs.skyvern_tab_list()

    assert listed_again["ok"] is True
    assert listed_again["data"]["active_tab_id"] == str(id(page_b))


@pytest.mark.asyncio
async def test_tab_list_preserves_selection_lost_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)
    ctx = BrowserContext(mode="local")
    state = _make_session_state(browser)
    state.selection_lost = True
    _patch_session(monkeypatch, state)
    monkeypatch.setattr(mcp_tabs, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
    monkeypatch.setattr(mcp_tabs, "ensure_browser_hooks", MagicMock())

    result = await mcp_tabs.skyvern_tab_list()

    assert result["ok"] is True
    assert result["data"]["active_tab_id"] is None
    assert state.selection_lost is True
    assert state._implicit_page is None


@pytest.mark.asyncio
async def test_tab_list_answers_within_its_bound_when_a_page_title_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_returns() -> str:
        await asyncio.sleep(3600)
        return "Slow"

    hanging = _make_mock_page("https://slow.com", "Slow")
    hanging.title = _never_returns
    browser = _make_mock_browser(hanging)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, hanging, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))
    monkeypatch.setattr(mcp_tabs, "TAB_TITLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", 50)

    result = await asyncio.wait_for(mcp_tabs.skyvern_tab_list(), timeout=5)

    assert result["ok"] is True
    assert result["data"]["tabs"][0]["title"] == ""
    assert result["data"]["tabs"][0]["url"] == "https://slow.com"


@pytest.mark.asyncio
async def test_tab_list_spends_one_title_budget_across_all_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_returns() -> str:
        await asyncio.sleep(3600)
        return "Slow"

    pages = [_make_mock_page(f"https://slow{i}.com", "Slow") for i in range(3)]
    for page in pages:
        page.title = _never_returns
    browser = _make_mock_browser(*pages)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, pages[0], ctx)
    _patch_session(monkeypatch, _make_session_state(browser))
    monkeypatch.setattr(mcp_tabs, "TAB_TITLE_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(mcp_tabs, "DEFAULT_ACTION_TIMEOUT_MS", 200)
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", 50)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await mcp_tabs.skyvern_tab_list()
    elapsed = loop.time() - started

    assert result["ok"] is True
    assert [tab["title"] for tab in result["data"]["tabs"]] == ["", "", ""]
    assert [tab["url"] for tab in result["data"]["tabs"]] == [f"https://slow{i}.com" for i in range(3)]
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_tab_list_reports_debugger_attachment_only_in_extension_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "Page A")
    page_b = _make_mock_page("https://b.com", "Page B")
    browser = _make_mock_browser(page_a, page_b)
    ctx = BrowserContext(mode="extension", can_access_localhost=True)
    _patch_get_page(monkeypatch, page_a, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    runtime = MagicMock()
    runtime.page_debugger_attached = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(mcp_tabs.BrowserExtensionRuntime, "instance", MagicMock(return_value=runtime))

    result = await mcp_tabs.skyvern_tab_list()

    assert result["ok"] is True
    assert [tab["debugger_attached"] for tab in result["data"]["tabs"]] == [True, False]
    assert runtime.page_debugger_attached.await_args_list[0].args == (page_a,)
    assert runtime.page_debugger_attached.await_args_list[1].args == (page_b,)


@pytest.mark.asyncio
async def test_tab_list_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_tabs, "resolve_browser", AsyncMock(side_effect=mcp_tabs.BrowserNotAvailableError()))

    result = await mcp_tabs.skyvern_tab_list()

    assert result["ok"] is False
    assert result["error"]["code"] == "NO_ACTIVE_BROWSER"


@pytest.mark.asyncio
async def test_tab_management_recovers_from_stale_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    selected_page = _make_mock_page("https://selected.example", "Selected", closed=True)
    remaining_page = _make_mock_page("https://remaining.example", "Remaining")
    new_page = _make_mock_page("about:blank", "New tab")
    browser = _make_mock_browser(remaining_page)
    ctx = BrowserContext(mode="local")
    state = _make_session_state(browser)
    state._active_page = selected_page
    state.selection_lost = True
    _patch_session(monkeypatch, state)

    resolve = AsyncMock(return_value=(browser, ctx))
    get_page = AsyncMock(side_effect=AssertionError("tab management must not resolve a page"))
    monkeypatch.setattr(mcp_tabs, "resolve_browser", resolve)
    monkeypatch.setattr(mcp_tabs, "get_page", get_page)

    listed = await mcp_tabs.skyvern_tab_list()

    assert listed["ok"] is True
    assert listed["data"]["active_tab_id"] is None
    assert listed["data"]["tabs"][0]["is_active"] is False
    assert state._active_page is selected_page

    switched = await mcp_tabs.skyvern_tab_switch(tab_id=str(id(remaining_page)))

    assert switched["ok"] is True
    assert state._active_page is remaining_page

    state._active_page = selected_page
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    browser._browser_context.pages = [remaining_page, new_page]
    created = await mcp_tabs.skyvern_tab_new()

    assert created["ok"] is True
    assert state._active_page is new_page
    assert resolve.await_count == 3
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_tab_close_nonselected_tab_does_not_require_stale_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_page = _make_mock_page("https://selected.example", "Selected", closed=True)
    target_page = _make_mock_page("https://target.example", "Target")
    browser = _make_mock_browser(target_page)
    ctx = BrowserContext(mode="local")
    state = _make_session_state(browser)
    state._active_page = selected_page
    state.selection_lost = True
    _patch_session(monkeypatch, state)
    monkeypatch.setattr(mcp_tabs, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
    get_page = AsyncMock(side_effect=AssertionError("non-selected tab close must not resolve a page"))
    monkeypatch.setattr(mcp_tabs, "get_page", get_page)

    def _close_side_effect() -> None:
        browser._browser_context.pages = []

    target_page.close = AsyncMock(side_effect=_close_side_effect)
    result = await mcp_tabs.skyvern_tab_close(tab_id=str(id(target_page)))

    assert result["ok"] is True
    assert result["data"]["closed_tab_id"] == str(id(target_page))
    assert state._active_page is selected_page
    assert state.selection_lost is True
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_tab_tool_preserves_page_selection_lost_error(monkeypatch: pytest.MonkeyPatch) -> None:
    active_page = _make_mock_page(closed=True)
    browser = _make_mock_browser(active_page)
    ctx = BrowserContext(mode="local")
    state = _make_session_state(browser)
    state.context = ctx
    state._active_page = active_page
    monkeypatch.setattr(mcp_tabs, "get_current_session", lambda: state)
    monkeypatch.setattr(session_manager, "get_current_session", lambda: state)
    monkeypatch.setattr(session_manager, "resolve_browser", AsyncMock(return_value=(browser, ctx)))

    result = await mcp_tabs.skyvern_tab_close()

    assert result["ok"] is False
    assert result["error"]["code"] == "PAGE_SELECTION_LOST"
    assert result["error"]["message"] == "The active page is closed or detached"
    assert result["error"]["hint"] == "Call skyvern_tab_list, then skyvern_tab_switch or skyvern_tab_new"


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
    _patch_get_page(monkeypatch, existing_page, ctx)

    state = _make_session_state(browser)
    state.selection_lost = True
    _patch_session(monkeypatch, state)

    # After new_page(), browser.pages should include both
    browser._browser_context.pages = [existing_page, new_page]

    result = await mcp_tabs.skyvern_tab_new()

    assert result["ok"] is True
    assert result["data"]["is_active"] is True
    assert state._active_page is new_page
    assert state.selection_lost is False
    browser._browser_context.new_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_tab_new_initializes_popup_and_inspection_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    existing_page = _make_mock_page("https://old.com", "Old")
    new_page = _make_mock_page("about:blank", "New Tab")
    popup = _make_mock_page("https://popup.com", "Popup")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)

    ctx = BrowserContext(mode="local")
    state = _make_session_state(browser)
    state.context = ctx
    session_manager.set_current_session(state)
    monkeypatch.setattr(mcp_tabs, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
    browser.get_page_for = AsyncMock(return_value=SimpleNamespace(page=new_page))

    result = await mcp_tabs.skyvern_tab_new()

    assert result["ok"] is True
    assert browser._browser_context.on.call_count == 1
    page_event_handler = browser._browser_context.on.call_args.args[1]
    handlers = {call.args[0]: call.args[1] for call in existing_page.on.call_args_list}
    assert {"console", "response", "dialog", "pageerror"} <= handlers.keys()

    handlers["console"](SimpleNamespace(type="warning", text="early console", location={}))
    response = SimpleNamespace(
        url="https://api.example.test/data",
        request=SimpleNamespace(method="GET", timing={}, resource_type="fetch"),
        status=200,
        headers={},
    )
    handlers["response"](response)
    assert any(entry["text"] == "early console" for entry in state.console_messages)
    assert any(entry["url"] == "https://api.example.test/data" for entry in state.network_requests)

    browser._browser_context.pages = [existing_page, new_page, popup]
    page_event_handler(popup)

    waited = await mcp_tabs.skyvern_tab_wait_for_new(timeout_ms=1000)

    assert waited["ok"] is True
    assert waited["data"]["tab_id"] == str(id(popup))


@pytest.mark.asyncio
async def test_tab_new_with_url(monkeypatch: pytest.MonkeyPatch, public_dns_resolver: None) -> None:
    existing_page = _make_mock_page()
    new_page = _make_mock_page("https://target.com", "Target")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    browser._browser_context.pages = [existing_page, new_page]

    ctx = BrowserContext(mode="cloud_session", session_id="pbs_test", can_access_localhost=False)
    _patch_get_page(monkeypatch, existing_page, ctx)

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)
    do_navigate = AsyncMock(
        return_value=NavigateResult(url="https://target.com/", title="Target", load_state="domcontentloaded")
    )
    monkeypatch.setattr(mcp_tabs, "do_navigate", do_navigate)

    result = await mcp_tabs.skyvern_tab_new(url="https://target.com")

    assert result["ok"] is True
    assert result["data"]["title"] == "Target"
    do_navigate.assert_awaited_once_with(
        new_page,
        "https://target.com/",
        timeout=30000,
        wait_until="domcontentloaded",
        can_access_localhost=False,
        is_localhost_destination=False,
    )


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
    do_navigate = AsyncMock(return_value=NavigateResult(url=url, title="Local", load_state="domcontentloaded"))
    monkeypatch.setattr(mcp_tabs, "do_navigate", do_navigate)

    result = await mcp_tabs.skyvern_tab_new(url=url)

    assert result["ok"] is True
    do_navigate.assert_awaited_once_with(
        new_page,
        url,
        timeout=30000,
        wait_until="domcontentloaded",
        can_access_localhost=True,
        is_localhost_destination=True,
    )


@pytest.mark.asyncio
async def test_tab_new_navigation_failure_retains_new_active_tab(
    monkeypatch: pytest.MonkeyPatch, public_dns_resolver: None
) -> None:
    existing_page = _make_mock_page("https://old.com", "Old")
    new_page = _make_mock_page("about:blank", "New Tab")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    browser._browser_context.pages = [existing_page, new_page]

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, existing_page, ctx)

    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)
    monkeypatch.setattr(mcp_tabs, "do_navigate", AsyncMock(side_effect=Exception("Navigation failed")))

    result = await mcp_tabs.skyvern_tab_new(url="https://example.com")

    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_FAILED"
    assert result["error"]["details"] == {"tab_id": str(id(new_page))}
    assert str(id(new_page)) in result["error"]["hint"]
    assert "remains open and active" in result["error"]["hint"]
    assert state._active_page is new_page
    new_page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_tab_new_page_creation_failure_restores_previous_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_page = _make_mock_page("https://old.com", "Old")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(side_effect=Exception("creation failed"))

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, existing_page, ctx)
    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)

    result = await mcp_tabs.skyvern_tab_new()

    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_FAILED"
    assert "tab_id" not in result["error"]["details"]
    assert "could not be created" in result["error"]["hint"]
    assert "previous active tab was unchanged" in result["error"]["hint"]
    assert state._active_page is existing_page


@pytest.mark.asyncio
async def test_tab_new_navigation_failure_restores_previous_tab_if_new_tab_closes(
    monkeypatch: pytest.MonkeyPatch, public_dns_resolver: None
) -> None:
    existing_page = _make_mock_page("https://old.com", "Old")
    new_page = _make_mock_page("about:blank", "New Tab", closed=True)
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    browser._browser_context.pages = [existing_page]

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, existing_page, ctx)
    state = _make_session_state(browser)
    state._active_page = existing_page
    _patch_session(monkeypatch, state)
    monkeypatch.setattr(mcp_tabs, "do_navigate", AsyncMock(side_effect=Exception("Navigation failed")))

    result = await mcp_tabs.skyvern_tab_new(url="https://example.com")

    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_FAILED"
    assert "tab_id" not in result["error"]["details"]
    assert "closed during navigation" in result["error"]["hint"]
    assert state._active_page is existing_page


@pytest.mark.asyncio
async def test_tab_new_navigation_degradation_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, public_dns_resolver: None
) -> None:
    existing_page = _make_mock_page()
    new_page = _make_mock_page("https://target.com", "Ignored title")
    browser = _make_mock_browser(existing_page)
    browser._browser_context.new_page = AsyncMock(return_value=new_page)
    browser._browser_context.pages = [existing_page, new_page]

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, existing_page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))
    monkeypatch.setattr(
        mcp_tabs,
        "do_navigate",
        AsyncMock(return_value=NavigateResult(url="https://target.com/", title="Target", load_state="commit")),
    )

    result = await mcp_tabs.skyvern_tab_new(url="https://target.com")

    assert result["ok"] is True
    assert result["data"]["url"] == "https://target.com/"
    assert result["data"]["title"] == "Target"
    assert result["warnings"]
    assert "domcontentloaded" in result["warnings"][0]
    assert "commit" in result["warnings"][0]
    new_page.title.assert_not_awaited()


# ═══════════════════════════════════════════════════
# skyvern_tab_switch
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tab_switch_by_tab_id(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page_a, ctx)

    state = _make_session_state(browser)
    _patch_session(monkeypatch, state)

    target_id = str(id(page_b))
    state._implicit_page = page_a
    state.selection_lost = True
    result = await mcp_tabs.skyvern_tab_switch(tab_id=target_id)

    assert result["ok"] is True
    assert result["data"]["tab_id"] == target_id
    assert result["data"]["is_active"] is True
    assert state._active_page is page_b
    assert state._implicit_page is None
    assert state.selection_lost is False


@pytest.mark.asyncio
async def test_tab_switch_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page_a, ctx)

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
    _patch_get_page(monkeypatch, page_a, ctx)

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
    _patch_get_page(monkeypatch, page_a, ctx)

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
async def test_tab_close_non_active_popup_preserves_implicit_page(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    popup_b = _make_mock_page("https://b.com", "B")
    popup_c = _make_mock_page("https://c.com", "C")
    browser = _make_mock_browser(page_a, popup_b, popup_c)

    ctx = BrowserContext(mode="local")
    state = _make_session_state(browser)
    state._active_page = None
    state._implicit_page = page_a
    state._working_frame = MagicMock()
    _patch_session(monkeypatch, state)
    monkeypatch.setattr(mcp_tabs, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
    monkeypatch.setattr(mcp_tabs, "ensure_browser_hooks", MagicMock())
    clear = MagicMock()
    monkeypatch.setattr(mcp_tabs, "clear_session_ref_map", clear)

    def _close_side_effect() -> None:
        browser._browser_context.pages = [page_a, popup_b]

    popup_c.close = AsyncMock(side_effect=_close_side_effect)

    result = await mcp_tabs.skyvern_tab_close(tab_id=str(id(popup_c)))

    assert result["ok"] is True
    assert state._implicit_page is page_a
    assert state._working_frame is not None
    clear.assert_not_called()

    page_a_wrapper = SimpleNamespace(page=page_a)
    browser.get_page_for = AsyncMock(return_value=page_a_wrapper)
    browser.get_working_page = AsyncMock(return_value=SimpleNamespace(page=popup_b))
    monkeypatch.setattr(session_manager, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
    monkeypatch.setattr(session_manager, "get_current_session", lambda: state)
    monkeypatch.setattr(session_manager, "ensure_browser_hooks", MagicMock())

    page, _ = await session_manager.get_page()

    assert page is page_a_wrapper
    browser.get_page_for.assert_awaited_once_with(page_a)
    browser.get_working_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_tab_close_clears_implicit_page_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page_a, ctx)

    state = _make_session_state(browser)
    state._implicit_page = page_a
    state._working_frame = MagicMock()
    _patch_session(monkeypatch, state)
    clear = MagicMock()
    monkeypatch.setattr(mcp_tabs, "clear_session_ref_map", clear)

    def _close_side_effect() -> None:
        browser._browser_context.pages = [page_b]

    page_a.close = AsyncMock(side_effect=_close_side_effect)

    result = await mcp_tabs.skyvern_tab_close(tab_id=str(id(page_a)))

    assert result["ok"] is True
    assert state._implicit_page is None
    assert state._working_frame is None
    clear.assert_called_once_with(session_id=None, cdp_url=None)


@pytest.mark.asyncio
async def test_tab_close_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page("https://a.com", "A")
    page_b = _make_mock_page("https://b.com", "B")
    browser = _make_mock_browser(page_a, page_b)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page_a, ctx)

    state = _make_session_state(browser)
    state._working_frame = MagicMock()
    _patch_session(monkeypatch, state)
    clear = MagicMock()
    monkeypatch.setattr(mcp_tabs, "clear_session_ref_map", clear)

    def _close_side_effect() -> None:
        browser._browser_context.pages = [page_a]

    page_b.close = AsyncMock(side_effect=_close_side_effect)

    result = await mcp_tabs.skyvern_tab_close(index=1)

    assert result["ok"] is True
    assert result["data"]["closed_tab_id"] == str(id(page_b))
    assert result["data"]["remaining_tabs"] == 1
    assert state._working_frame is None
    clear.assert_called_once_with(session_id=None, cdp_url=None)

    listed = await mcp_tabs.skyvern_tab_list()
    assert listed["ok"] is True
    assert listed["data"]["active_tab_id"] == str(id(page_a))
    assert state._working_frame is None


@pytest.mark.asyncio
async def test_tab_close_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    page_a = _make_mock_page()
    browser = _make_mock_browser(page_a)

    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page_a, ctx)

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
        session_manager.set_current_session(SessionState(browser=MagicMock()))
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

        async def _resolve_browser_installing_registered(
            **_kwargs: object,
        ) -> tuple[MagicMock, BrowserContext]:
            session_manager.set_current_session(owned)
            return owned.browser, ctx

        monkeypatch.setattr(mcp_tabs, "resolve_browser", _resolve_browser_installing_registered)
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
    state.selection_lost = True

    result = await mcp_tabs.skyvern_open_tabs(
        urls=["https://a.com", "https://b.com"], screenshot=False, set_active_last=True
    )

    assert result["ok"] is True, result
    assert state._active_page is not None
    assert state._active_page.url == "https://b.com"
    assert state.selection_lost is False
    clear.assert_called_once()


@pytest.mark.asyncio
async def test_open_tabs_keeps_lost_selection_when_active_tab_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _patch_open_tabs(monkeypatch)
    previous_active = state._active_page
    implicit_page = _make_mock_page("https://implicit.example", "Implicit")
    state._implicit_page = implicit_page
    state.selection_lost = True

    result = await mcp_tabs.skyvern_open_tabs(urls=["https://a.com"], screenshot=False)

    assert result["ok"] is True
    assert state.selection_lost is True
    assert state._implicit_page is implicit_page
    assert state._active_page is previous_active


@pytest.mark.asyncio
async def test_open_tabs_clears_lost_selection_when_active_tab_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _patch_open_tabs(monkeypatch)
    state.selection_lost = True

    result = await mcp_tabs.skyvern_open_tabs(urls=["https://a.com"], screenshot=False, set_active_last=True)

    assert result["ok"] is True
    assert state.selection_lost is False
    assert state._active_page.url == "https://a.com"
    assert state._implicit_page is None


@pytest.mark.asyncio
async def test_open_tabs_keeps_lost_selection_when_every_navigation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _patch_open_tabs(monkeypatch)
    for page in state.browser._browser_context.pages[1:]:
        page.goto.side_effect = RuntimeError("navigation failed")
    state.selection_lost = True

    result = await mcp_tabs.skyvern_open_tabs(
        urls=["https://a.com", "https://b.com"], screenshot=False, set_active_last=True
    )

    assert result["ok"] is True
    assert result["data"]["opened"] == 0
    assert state.selection_lost is True


@pytest.mark.asyncio
async def test_open_tabs_keeps_implicit_page_when_active_tab_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    state, _ = _patch_open_tabs(monkeypatch)
    pinned_page = state._active_page
    state._implicit_page = pinned_page

    result = await mcp_tabs.skyvern_open_tabs(urls=["https://a.com"], screenshot=False)

    assert result["ok"] is True
    assert state._implicit_page is pinned_page


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
