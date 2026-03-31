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

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState, get_current_session, set_current_session
from skyvern.cli.mcp_tools.browser import skyvern_frame_list, skyvern_frame_main, skyvern_frame_switch


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
  <iframe id="pay-frame" name="payment" srcdoc='
    <!DOCTYPE html>
    <html><body>
      <h2 id="frame-heading">Payment</h2>
      <input id="card" type="text" value="" placeholder="Card" />
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
        # Return a lightweight wrapper that has the frame methods
        return _WrappedPage(self._page)


class _WrappedPage:
    """Thin wrapper around Playwright Page to add frame_switch/main/list."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self._working_frame = None

    @property
    def _locator_scope(self) -> Any:
        if self._working_frame is not None:
            return self._working_frame
        return self.page

    async def frame_switch(
        self, *, selector: str | None = None, name: str | None = None, index: int | None = None
    ) -> dict[str, Any]:
        params = sum(p is not None for p in (selector, name, index))
        if params != 1:
            raise ValueError("Exactly one of selector, name, or index is required.")

        frame = None
        if selector is not None:
            element = await self.page.query_selector(selector)
            if element is None:
                raise ValueError(f"Selector '{selector}' did not match any element.")
            frame = await element.content_frame()
            if frame is None:
                raise ValueError(f"Selector '{selector}' did not resolve to an iframe.")
        elif name is not None:
            frame = self.page.frame(name=name)
            if frame is None:
                raise ValueError(f"No frame found with name '{name}'.")
        elif index is not None:
            frames = self.page.frames
            if index < 0 or index >= len(frames):
                raise ValueError(f"Frame index {index} out of range.")
            frame = frames[index]

        self._working_frame = frame
        return {"name": frame.name if frame else None, "url": frame.url if frame else None}

    def frame_main(self) -> dict[str, str]:
        self._working_frame = None
        return {"status": "switched_to_main_frame"}

    async def frame_list(self) -> list[dict[str, Any]]:
        return [
            {"index": i, "name": f.name, "url": f.url, "is_main": f == self.page.main_frame}
            for i, f in enumerate(self.page.frames)
        ]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.page, name)


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
