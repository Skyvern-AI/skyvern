"""End-to-end tests for tab management MCP tools with a real Playwright browser.

These tests exercise the full tab management stack:
  SessionState → get_page() → SkyvernBrowser → Playwright BrowserContext

No LLM key required — tab operations are pure browser automation.
Requires Playwright browsers installed (run: playwright install chromium).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


@pytest_asyncio.fixture
async def browser_session():
    """Launch a real local headless browser and wire up SessionState for MCP tools."""
    from skyvern import Skyvern
    from skyvern.cli.core.result import BrowserContext
    from skyvern.cli.core.session_manager import SessionState, set_current_session

    skyvern = Skyvern.local(use_in_memory_db=True)
    browser = await skyvern.launch_local_browser(headless=True)

    state = SessionState(
        browser=browser,
        context=BrowserContext(mode="local"),
    )
    set_current_session(state)

    yield state, browser

    try:
        await browser.close()
    except Exception:
        pass
    set_current_session(SessionState())
    await skyvern.aclose()


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_list_single_tab(browser_session) -> None:
    """Fresh browser has exactly one tab."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_list

    result = await skyvern_tab_list()

    assert result["ok"] is True
    assert result["data"]["count"] == 1
    tab = result["data"]["tabs"][0]
    assert tab["index"] == 0
    assert tab["is_active"] is True
    assert tab["tab_id"]  # non-empty string


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_new_and_list(browser_session) -> None:
    """Open a new tab and verify tab_list shows 2 tabs."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_list, skyvern_tab_new

    new_result = await skyvern_tab_new()
    assert new_result["ok"] is True
    assert new_result["data"]["is_active"] is True
    new_tab_id = new_result["data"]["tab_id"]

    list_result = await skyvern_tab_list()
    assert list_result["ok"] is True
    assert list_result["data"]["count"] == 2

    # The new tab should be active
    active_tabs = [t for t in list_result["data"]["tabs"] if t["is_active"]]
    assert len(active_tabs) == 1
    assert active_tabs[0]["tab_id"] == new_tab_id


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_new_with_navigation(browser_session) -> None:
    """Open a new tab with a URL and verify navigation."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_new

    result = await skyvern_tab_new(url="https://example.com")

    assert result["ok"] is True
    assert "example.com" in result["data"]["url"]
    assert result["data"]["title"]  # Should have a title


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_switch_and_verify(browser_session) -> None:
    """Open two tabs, switch between them, verify active tab changes."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_list, skyvern_tab_new, skyvern_tab_switch

    state, browser = browser_session

    # Get original tab ID
    initial_list = await skyvern_tab_list()
    first_tab_id = initial_list["data"]["tabs"][0]["tab_id"]

    # Open second tab
    await skyvern_tab_new(url="https://example.com")

    # Switch back to first tab
    switch_result = await skyvern_tab_switch(tab_id=first_tab_id)
    assert switch_result["ok"] is True
    assert switch_result["data"]["tab_id"] == first_tab_id
    assert switch_result["data"]["is_active"] is True

    # Verify via tab_list that first tab is now active
    list_result = await skyvern_tab_list()
    active = [t for t in list_result["data"]["tabs"] if t["is_active"]]
    assert active[0]["tab_id"] == first_tab_id


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_switch_by_index(browser_session) -> None:
    """Switch tabs using index instead of tab_id."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_new, skyvern_tab_switch

    await skyvern_tab_new()

    result = await skyvern_tab_switch(index=0)
    assert result["ok"] is True
    assert result["data"]["index"] == 0


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_close_active(browser_session) -> None:
    """Close the active tab; remaining tab becomes the new working page."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_close, skyvern_tab_list, skyvern_tab_new

    state, browser = browser_session

    # Open second tab (becomes active)
    new_result = await skyvern_tab_new()
    new_tab_id = new_result["data"]["tab_id"]

    # Close the active (second) tab
    close_result = await skyvern_tab_close()
    assert close_result["ok"] is True
    assert close_result["data"]["closed_tab_id"] == new_tab_id
    assert close_result["data"]["remaining_tabs"] == 1

    # Verify only one tab remains
    list_result = await skyvern_tab_list()
    assert list_result["data"]["count"] == 1


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_close_by_index(browser_session) -> None:
    """Close a specific tab by index."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_close, skyvern_tab_list, skyvern_tab_new

    await skyvern_tab_new()
    list_before = await skyvern_tab_list()
    assert list_before["data"]["count"] == 2

    close_result = await skyvern_tab_close(index=1)
    assert close_result["ok"] is True
    assert close_result["data"]["remaining_tabs"] == 1


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_close_last_tab_recovers(browser_session) -> None:
    """Closing the last tab should still allow get_working_page to recover."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_close

    state, browser = browser_session

    # Close the only tab
    close_result = await skyvern_tab_close()
    assert close_result["ok"] is True

    # get_working_page() should lazily create a new page
    page = await browser.get_working_page()
    assert page is not None


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_switch_not_found(browser_session) -> None:
    """Switching to a non-existent tab returns an error."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_switch

    result = await skyvern_tab_switch(tab_id="does_not_exist_12345")
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_close_not_found(browser_session) -> None:
    """Closing a non-existent tab returns an error."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_close

    result = await skyvern_tab_close(tab_id="does_not_exist_12345")
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_wait_for_new_popup(browser_session) -> None:
    """Open a popup via JS and verify tab_wait_for_new catches it."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_list, skyvern_tab_wait_for_new

    state, browser = browser_session

    # Navigate to about:blank first
    page = await browser.get_working_page()
    await page.page.goto("about:blank")

    # Use JS to open a popup after a short delay
    async def _open_popup():
        await asyncio.sleep(0.3)
        await page.page.evaluate("window.open('about:blank', '_blank')")

    task = asyncio.create_task(_open_popup())

    result = await skyvern_tab_wait_for_new(timeout_ms=5000)

    assert result["ok"] is True
    assert result["data"]["is_active"] is False  # Does NOT auto-switch (Decision 4A)
    assert result["data"]["tab_id"]

    await task

    # Verify 2 tabs now exist
    list_result = await skyvern_tab_list()
    assert list_result["data"]["count"] == 2


@pytest.mark.asyncio
@_skip_no_browser
async def test_tab_wait_timeout(browser_session) -> None:
    """No popup opened — should timeout."""
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_wait_for_new

    result = await skyvern_tab_wait_for_new(timeout_ms=1000)
    assert result["ok"] is False
    assert result["error"]["code"] == "TIMEOUT"


@pytest.mark.asyncio
@_skip_no_browser
async def test_full_multi_tab_workflow(browser_session) -> None:
    """Full workflow: list → new → navigate → switch → close → verify."""
    from skyvern.cli.mcp_tools.tabs import (
        skyvern_tab_close,
        skyvern_tab_list,
        skyvern_tab_new,
        skyvern_tab_switch,
    )

    # 1. Initial state: 1 tab
    r = await skyvern_tab_list()
    assert r["data"]["count"] == 1
    tab0_id = r["data"]["tabs"][0]["tab_id"]

    # 2. Open a new tab with a URL
    r = await skyvern_tab_new(url="https://example.com")
    assert r["ok"] is True
    tab1_id = r["data"]["tab_id"]

    # 3. List shows 2 tabs, tab1 is active
    r = await skyvern_tab_list()
    assert r["data"]["count"] == 2
    active = [t for t in r["data"]["tabs"] if t["is_active"]]
    assert active[0]["tab_id"] == tab1_id

    # 4. Switch back to tab0
    r = await skyvern_tab_switch(tab_id=tab0_id)
    assert r["ok"] is True

    # 5. Verify tab0 is now active
    r = await skyvern_tab_list()
    active = [t for t in r["data"]["tabs"] if t["is_active"]]
    assert active[0]["tab_id"] == tab0_id

    # 6. Close tab1 by ID
    r = await skyvern_tab_close(tab_id=tab1_id)
    assert r["ok"] is True

    # 7. Verify back to 1 tab
    r = await skyvern_tab_list()
    assert r["data"]["count"] == 1


@pytest.mark.asyncio
@_skip_no_browser
async def test_multipage_inspection_hooks_capture_from_both_tabs(browser_session) -> None:
    """Console messages from multiple tabs are captured with tab_id attribution."""
    from skyvern.cli.mcp_tools.inspection import skyvern_console_messages
    from skyvern.cli.mcp_tools.tabs import skyvern_tab_list, skyvern_tab_new

    state, browser = browser_session

    # Ensure tab list works before proceeding
    r = await skyvern_tab_list()
    assert r["data"]["count"] == 1

    # Navigate first tab and log something
    page0 = await browser.get_working_page()
    await page0.page.goto("about:blank")
    await page0.page.evaluate("console.log('hello from tab 0')")

    # Open second tab and log there too
    await skyvern_tab_new()
    page1_raw = state._active_page
    if page1_raw:
        await page1_raw.goto("about:blank")
        await page1_raw.evaluate("console.log('hello from tab 1')")

    # Wait briefly for async event listeners
    await asyncio.sleep(0.2)

    # Check console messages — should have entries from both tabs
    result = await skyvern_console_messages()
    assert result["ok"] is True, f"Console messages tool failed: {result.get('error')}"
    messages = result["data"]["messages"]
    assert len(messages) >= 2  # Messages from both tabs
    # Verify tab_id attribution — messages from distinct tabs should have different tab_ids
    tab_ids = {m["tab_id"] for m in messages if "tab_id" in m}
    assert len(tab_ids) >= 2, f"Expected messages from 2+ tabs, got tab_ids: {tab_ids}"
