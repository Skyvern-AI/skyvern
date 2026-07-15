"""E2E test for iframe MCP tools with a real browser.

Exercises the MCP tool chain (frame_list, frame_switch, frame_main) through
real Playwright + SessionState wiring, without requiring Skyvern's local
browser launcher infrastructure.

Skipped in CI when Playwright browsers are not installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState, get_current_session, set_current_session
from skyvern.cli.mcp_tools.browser import (
    skyvern_execute,
    skyvern_frame_list,
    skyvern_frame_main,
    skyvern_frame_switch,
    skyvern_observe,
)
from skyvern.library.skyvern_browser_page import SkyvernBrowserPage


def _has_playwright_browser() -> bool:
    """Check that Playwright's chromium binary exists for the current installed version."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)

pytestmark = _skip_no_browser

MAIN_HTML = """\
<!DOCTYPE html>
<html>
<body>
  <h1 id="main-heading">Main Page</h1>
  <input id="main-input" type="text" value="" />
  <button id="parent-action" type="button">Parent action</button>
  <iframe id="pay-frame" name="payment" srcdoc='
    <!DOCTYPE html>
    <html><body>
      <h2 id="frame-heading">Payment</h2>
      <input id="card" type="text" value="" placeholder="Card" />
      <button id="frame-action" type="button"
        onclick="document.getElementById(`frame-status`).textContent = `clicked`">
        Frame action
      </button>
      <div id="frame-status">idle</div>
    </body></html>
  '></iframe>
</body>
</html>
"""


class _FakeBrowserContext:
    """Minimal browser context to satisfy get_page() hooks from tab management."""

    def __init__(self, page: Any) -> None:
        self.pages = [page]

    def on(self, event: str, handler: Any) -> None:
        pass  # No-op for tests


class _FakeBrowser:
    """Minimal SkyvernBrowser substitute that wraps a real Playwright page."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._browser_context = _FakeBrowserContext(page)

    async def get_working_page(self) -> Any:
        # The real page wrapper, so frame routing (_locator_scope) matches production.
        return SkyvernBrowserPage(MagicMock(), self._page)


@pytest_asyncio.fixture
async def mcp_session():
    """Set up a real Playwright browser and wire it into SessionState."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception:
            pytest.skip("Playwright chromium binary not available")
        context = await browser.new_context()
        pw_page = await context.new_page()
        await pw_page.set_content(MAIN_HTML)
        await asyncio.sleep(0.3)

        fake_browser = _FakeBrowser(pw_page)
        ctx = BrowserContext(mode="local")
        state = SessionState(browser=fake_browser, context=ctx)  # type: ignore[arg-type]
        set_current_session(state)

        yield state

        set_current_session(SessionState())
        await context.close()
        await browser.close()


# ---------------------------------------------------------------------------
# MCP tool e2e tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_frame_list_real_browser(mcp_session: SessionState) -> None:
    result = await skyvern_frame_list()
    assert result["ok"] is True
    frames = result["data"]["frames"]
    assert len(frames) >= 2
    names = [f["name"] for f in frames]
    assert "payment" in names
    assert result["data"]["count"] >= 2


@pytest.mark.asyncio
async def test_mcp_frame_switch_by_selector(mcp_session: SessionState) -> None:
    result = await skyvern_frame_switch(selector="#pay-frame")
    assert result["ok"] is True
    assert result["data"]["frame_name"] == "payment"
    assert result["data"]["switched_by"] == "selector"

    # Verify SessionState was updated
    assert mcp_session._working_frame is not None


@pytest.mark.asyncio
async def test_mcp_frame_switch_by_name(mcp_session: SessionState) -> None:
    result = await skyvern_frame_switch(name="payment")
    assert result["ok"] is True
    assert result["data"]["switched_by"] == "name"
    assert mcp_session._working_frame is not None


@pytest.mark.asyncio
async def test_mcp_frame_main_clears_state(mcp_session: SessionState) -> None:
    # Switch in first
    await skyvern_frame_switch(selector="#pay-frame")
    assert mcp_session._working_frame is not None

    # Switch back
    result = await skyvern_frame_main()
    assert result["ok"] is True
    assert mcp_session._working_frame is None


@pytest.mark.asyncio
async def test_mcp_frame_switch_invalid_selector(mcp_session: SessionState) -> None:
    result = await skyvern_frame_switch(selector="#nonexistent")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_mcp_frame_switch_persists_across_calls(mcp_session: SessionState) -> None:
    """Frame state set by frame_switch persists across subsequent get_page() calls."""
    # Switch into iframe
    await skyvern_frame_switch(selector="#pay-frame")

    # Simulate a subsequent MCP call — get_page() reads _working_frame from SessionState
    state = get_current_session()
    assert state._working_frame is not None

    # The next get_page() call would set page._working_frame from state._working_frame
    # Verify the state is there for the propagation
    frame = state._working_frame
    heading = await frame.locator("#frame-heading").text_content()
    assert heading == "Payment"


@pytest.mark.asyncio
async def test_mcp_observe_execute_ref_in_working_frame(mcp_session: SessionState) -> None:
    await skyvern_frame_switch(selector="#pay-frame")

    observe_result = await skyvern_observe()

    assert observe_result["ok"] is True
    names = {element["name"] for element in observe_result["data"]["elements"]}
    assert "Frame action" in names
    assert "Parent action" not in names
    frame = mcp_session._working_frame
    assert frame is not None
    assert observe_result["data"]["url"] == frame.url
    ref = next(element["ref"] for element in observe_result["data"]["elements"] if element["name"] == "Frame action")

    execute_result = await skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

    assert execute_result["ok"] is True
    assert await frame.locator("#frame-status").text_content() == "clicked"


@pytest.mark.asyncio
async def test_mcp_frame_main_invalidates_iframe_observe_ref(mcp_session: SessionState) -> None:
    await skyvern_frame_switch(selector="#pay-frame")
    observe_result = await skyvern_observe()
    ref = next(element["ref"] for element in observe_result["data"]["elements"] if element["name"] == "Frame action")
    frame = mcp_session._working_frame
    assert frame is not None

    await skyvern_frame_main()
    execute_result = await skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

    assert execute_result["ok"] is False
    assert "Unknown ref" in execute_result["data"]["results"][0]["error"]
    assert await frame.locator("#frame-status").text_content() == "idle"


@pytest.mark.asyncio
async def test_iframe_navigation_invalidates_observed_ref(mcp_session: SessionState) -> None:
    await skyvern_frame_switch(selector="#pay-frame")
    observe_result = await skyvern_observe()
    ref = next(element["ref"] for element in observe_result["data"]["elements"] if element["name"] == "Frame action")
    frame = mcp_session._working_frame
    assert frame is not None

    await frame.goto("data:text/html,<button id='replacement'>Replacement action</button>")
    execute_result = await skyvern_execute(steps=[{"tool": "click", "params": {"ref": ref}}])

    assert execute_result["ok"] is False
    assert "Unknown ref" in execute_result["data"]["results"][0]["error"]
