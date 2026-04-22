"""E2E tests for iframe support using a real headless Chromium browser.

Tests the full stack: SkyvernPage._locator_scope, frame_switch/main/list on
SkyvernBrowserPage, and the MCP tool functions — all against real iframes.

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

from skyvern.core.script_generations.skyvern_page_ai import SkyvernPageAi
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

# HTML fixture: main page with a form AND an iframe containing another form.
MAIN_HTML = """\
<!DOCTYPE html>
<html>
<body>
  <h1 id="main-heading">Main Page</h1>
  <input id="main-input" type="text" value="" placeholder="Main input" />
  <button id="main-btn" onclick="document.getElementById('main-input').value='clicked'">Main Button</button>

  <iframe id="payment-frame" name="payment" srcdoc='
    <!DOCTYPE html>
    <html><body>
      <h2 id="iframe-heading">Payment Form</h2>
      <input id="card-number" type="text" value="" placeholder="Card number" />
      <input id="card-name" type="text" value="" placeholder="Name on card" />
      <button id="pay-btn" onclick="document.getElementById(&apos;card-number&apos;).value=&apos;paid&apos;">Pay</button>
    </body></html>
  '></iframe>

  <iframe id="empty-frame" name="empty" srcdoc="<html><body><p>Empty</p></body></html>"></iframe>
</body>
</html>
"""


class _NoopAi(SkyvernPageAi):
    """Stub AI that raises if called — e2e tests use direct selectors only."""

    def __init__(self) -> None:
        pass

    async def ai_click(self, **kwargs: Any) -> Any:
        raise NotImplementedError("AI should not be called in e2e tests")

    async def ai_input_text(self, **kwargs: Any) -> Any:
        raise NotImplementedError("AI should not be called in e2e tests")

    async def ai_act(self, prompt: str) -> Any:
        raise NotImplementedError("AI should not be called in e2e tests")


class _TestPage(SkyvernBrowserPage):
    """SkyvernBrowserPage that doesn't need a full SkyvernBrowser."""

    def __init__(self, page: Any) -> None:
        # Use SkyvernPage.__init__ directly to avoid SkyvernBrowser dependency
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        SkyvernPage.__init__(self, page, _NoopAi())
        self._browser = MagicMock()
        self.agent = MagicMock()


@pytest_asyncio.fixture
async def browser_page():
    """Launch a real headless Chromium and set up the test page."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        pw_page = await context.new_page()
        await pw_page.set_content(MAIN_HTML)
        # Wait for iframes to load
        await pw_page.wait_for_selector("#payment-frame")
        await asyncio.sleep(0.3)

        page = _TestPage(pw_page)
        yield page

        await context.close()
        await browser.close()


# ---------------------------------------------------------------------------
# E2E: _locator_scope with real iframes
# ---------------------------------------------------------------------------


class TestLocatorScopeE2E:
    @pytest.mark.asyncio
    async def test_locator_scope_defaults_to_page(self, browser_page: _TestPage) -> None:
        heading = await browser_page._locator_scope.locator("#main-heading").text_content()
        assert heading == "Main Page"

    @pytest.mark.asyncio
    async def test_locator_scope_scopes_to_frame(self, browser_page: _TestPage) -> None:
        # Switch to iframe
        await browser_page.frame_switch(selector="#payment-frame")
        heading = await browser_page._locator_scope.locator("#iframe-heading").text_content()
        assert heading == "Payment Form"

    @pytest.mark.asyncio
    async def test_main_page_element_not_found_in_iframe(self, browser_page: _TestPage) -> None:
        await browser_page.frame_switch(selector="#payment-frame")
        count = await browser_page._locator_scope.locator("#main-heading").count()
        assert count == 0

    @pytest.mark.asyncio
    async def test_iframe_element_not_found_on_main_page(self, browser_page: _TestPage) -> None:
        count = await browser_page._locator_scope.locator("#card-number").count()
        assert count == 0


# ---------------------------------------------------------------------------
# E2E: frame_switch / frame_main / frame_list
# ---------------------------------------------------------------------------


class TestFrameSwitchE2E:
    @pytest.mark.asyncio
    async def test_switch_by_selector(self, browser_page: _TestPage) -> None:
        result = await browser_page.frame_switch(selector="#payment-frame")
        assert result["name"] == "payment"
        assert browser_page._working_frame is not None

    @pytest.mark.asyncio
    async def test_switch_by_name(self, browser_page: _TestPage) -> None:
        result = await browser_page.frame_switch(name="payment")
        assert result["name"] == "payment"
        assert browser_page._working_frame is not None

    @pytest.mark.asyncio
    async def test_switch_by_index(self, browser_page: _TestPage) -> None:
        # Index 0 = main frame, 1 = payment iframe, 2 = empty iframe
        result = await browser_page.frame_switch(index=1)
        assert result["name"] == "payment"

    @pytest.mark.asyncio
    async def test_switch_back_to_main(self, browser_page: _TestPage) -> None:
        await browser_page.frame_switch(selector="#payment-frame")
        assert browser_page._working_frame is not None

        browser_page.frame_main()
        assert browser_page._working_frame is None

        heading = await browser_page._locator_scope.locator("#main-heading").text_content()
        assert heading == "Main Page"

    @pytest.mark.asyncio
    async def test_switch_to_different_iframe(self, browser_page: _TestPage) -> None:
        await browser_page.frame_switch(name="payment")
        heading = await browser_page._locator_scope.locator("#iframe-heading").text_content()
        assert heading == "Payment Form"

        await browser_page.frame_switch(name="empty")
        text = await browser_page._locator_scope.locator("p").text_content()
        assert text == "Empty"


class TestFrameListE2E:
    @pytest.mark.asyncio
    async def test_lists_main_and_iframes(self, browser_page: _TestPage) -> None:
        frames = await browser_page.frame_list()
        assert len(frames) >= 3  # main + payment + empty
        assert frames[0]["is_main"] is True
        names = [f["name"] for f in frames]
        assert "payment" in names
        assert "empty" in names


# ---------------------------------------------------------------------------
# E2E: interactions INSIDE an iframe
# ---------------------------------------------------------------------------


class TestIframeInteractionE2E:
    @pytest.mark.asyncio
    async def test_fill_inside_iframe(self, browser_page: _TestPage) -> None:
        await browser_page.frame_switch(selector="#payment-frame")

        locator = browser_page._locator_scope.locator("#card-number")
        await locator.fill("4242424242424242")

        value = await locator.input_value()
        assert value == "4242424242424242"

    @pytest.mark.asyncio
    async def test_click_inside_iframe(self, browser_page: _TestPage) -> None:
        await browser_page.frame_switch(selector="#payment-frame")

        await browser_page._locator_scope.locator("#pay-btn").click()
        value = await browser_page._locator_scope.locator("#card-number").input_value()
        assert value == "paid"

    @pytest.mark.asyncio
    async def test_fill_main_page_unaffected_by_iframe(self, browser_page: _TestPage) -> None:
        # Fill in iframe
        await browser_page.frame_switch(selector="#payment-frame")
        await browser_page._locator_scope.locator("#card-number").fill("1234")

        # Switch back and verify main page
        browser_page.frame_main()
        main_value = await browser_page._locator_scope.locator("#main-input").input_value()
        assert main_value == ""  # untouched

    @pytest.mark.asyncio
    async def test_click_main_page_after_iframe(self, browser_page: _TestPage) -> None:
        await browser_page.frame_switch(selector="#payment-frame")
        await browser_page._locator_scope.locator("#card-number").fill("4242")
        browser_page.frame_main()

        await browser_page._locator_scope.locator("#main-btn").click()
        value = await browser_page._locator_scope.locator("#main-input").input_value()
        assert value == "clicked"


# ---------------------------------------------------------------------------
# E2E: SkyvernPage interaction methods inside iframe
# ---------------------------------------------------------------------------


class TestSkyvernPageMethodsInIframe:
    @pytest.mark.asyncio
    async def test_click_method_scopes_to_iframe(self, browser_page: _TestPage) -> None:
        """Test that SkyvernPage.click() with ai=None uses _locator_scope."""
        await browser_page.frame_switch(selector="#payment-frame")
        # Use click with ai=None and mode=direct to go through the direct path
        await browser_page.click("#pay-btn", ai=None, mode="direct")
        value = await browser_page._locator_scope.locator("#card-number").input_value()
        assert value == "paid"

    @pytest.mark.asyncio
    async def test_fill_method_scopes_to_iframe(self, browser_page: _TestPage) -> None:
        """Test that SkyvernPage.fill() with mode=direct uses _locator_scope."""
        await browser_page.frame_switch(selector="#payment-frame")
        await browser_page.fill("#card-name", "John Doe", ai=None, mode="direct")
        value = await browser_page._locator_scope.locator("#card-name").input_value()
        assert value == "John Doe"

    @pytest.mark.asyncio
    async def test_hover_method_scopes_to_iframe(self, browser_page: _TestPage) -> None:
        """Test that SkyvernPage.hover() uses _locator_scope."""
        await browser_page.frame_switch(selector="#payment-frame")
        # hover shouldn't throw — just verifying it resolves within frame
        await browser_page.hover("#pay-btn")


# ---------------------------------------------------------------------------
# E2E: CLI frame state persistence round-trip
#
# These tests exercise the actual _apply_cli_frame_state code path:
# save frame identifiers to CLIState (file-backed), create a FRESH page
# (simulating a new CLI invocation), call _apply_cli_frame_state, then
# verify actions target the iframe — not the main page.
# ---------------------------------------------------------------------------


class TestCLIFrameStatePersistence:
    @pytest.mark.asyncio
    async def test_cli_frame_state_reapplied_by_selector(self, browser_page: _TestPage, tmp_path: Any) -> None:
        """frame_switch by selector → save to CLIState → fresh page → _apply_cli_frame_state → in iframe."""
        import skyvern.cli.commands._state as state_mod
        from skyvern.cli.commands._state import CLIState, save_state
        from skyvern.cli.commands.browser import _apply_cli_frame_state

        # Patch STATE_FILE to a temp location so we don't touch the user's real state
        orig_file = state_mod.STATE_FILE
        state_mod.STATE_FILE = tmp_path / "state.json"
        try:
            # Simulate: user ran `skyvern browser frame switch --selector "#payment-frame"`
            save_state(CLIState(session_id="test", mode="cloud", frame_selector="#payment-frame"))

            # Simulate: new CLI invocation gets a FRESH page (no _working_frame)
            assert browser_page._working_frame is None

            # This is the code under test
            await _apply_cli_frame_state(browser_page)

            # Verify: page is now scoped to the iframe
            assert browser_page._working_frame is not None
            heading = await browser_page._locator_scope.locator("#iframe-heading").text_content()
            assert heading == "Payment Form"

            # Verify: main page elements are NOT visible from the iframe scope
            count = await browser_page._locator_scope.locator("#main-heading").count()
            assert count == 0
        finally:
            state_mod.STATE_FILE = orig_file

    @pytest.mark.asyncio
    async def test_cli_frame_state_reapplied_by_name(self, browser_page: _TestPage, tmp_path: Any) -> None:
        """frame_switch by name → save to CLIState → fresh page → _apply_cli_frame_state → in iframe."""
        import skyvern.cli.commands._state as state_mod
        from skyvern.cli.commands._state import CLIState, save_state
        from skyvern.cli.commands.browser import _apply_cli_frame_state

        orig_file = state_mod.STATE_FILE
        state_mod.STATE_FILE = tmp_path / "state.json"
        try:
            save_state(CLIState(session_id="test", mode="cloud", frame_name="payment"))

            assert browser_page._working_frame is None
            await _apply_cli_frame_state(browser_page)

            assert browser_page._working_frame is not None
            heading = await browser_page._locator_scope.locator("#iframe-heading").text_content()
            assert heading == "Payment Form"
        finally:
            state_mod.STATE_FILE = orig_file

    @pytest.mark.asyncio
    async def test_cli_frame_state_reapplied_by_index(self, browser_page: _TestPage, tmp_path: Any) -> None:
        """frame_switch by index → save to CLIState → fresh page → _apply_cli_frame_state → in iframe."""
        import skyvern.cli.commands._state as state_mod
        from skyvern.cli.commands._state import CLIState, save_state
        from skyvern.cli.commands.browser import _apply_cli_frame_state

        orig_file = state_mod.STATE_FILE
        state_mod.STATE_FILE = tmp_path / "state.json"
        try:
            # Index 1 = payment iframe (0 = main frame)
            save_state(CLIState(session_id="test", mode="cloud", frame_index=1))

            assert browser_page._working_frame is None
            await _apply_cli_frame_state(browser_page)

            assert browser_page._working_frame is not None
            heading = await browser_page._locator_scope.locator("#iframe-heading").text_content()
            assert heading == "Payment Form"
        finally:
            state_mod.STATE_FILE = orig_file

    @pytest.mark.asyncio
    async def test_cli_frame_state_noop_when_no_frame(self, browser_page: _TestPage, tmp_path: Any) -> None:
        """No frame state in CLIState → _apply_cli_frame_state is a no-op."""
        import skyvern.cli.commands._state as state_mod
        from skyvern.cli.commands._state import CLIState, save_state
        from skyvern.cli.commands.browser import _apply_cli_frame_state

        orig_file = state_mod.STATE_FILE
        state_mod.STATE_FILE = tmp_path / "state.json"
        try:
            save_state(CLIState(session_id="test", mode="cloud"))

            await _apply_cli_frame_state(browser_page)

            assert browser_page._working_frame is None
            heading = await browser_page._locator_scope.locator("#main-heading").text_content()
            assert heading == "Main Page"
        finally:
            state_mod.STATE_FILE = orig_file

    @pytest.mark.asyncio
    async def test_cli_frame_state_clears_on_stale_selector(self, browser_page: _TestPage, tmp_path: Any) -> None:
        """Stale selector in CLIState → _apply_cli_frame_state clears state and stays on main page."""
        import skyvern.cli.commands._state as state_mod
        from skyvern.cli.commands._state import CLIState, load_state, save_state
        from skyvern.cli.commands.browser import _apply_cli_frame_state

        orig_file = state_mod.STATE_FILE
        state_mod.STATE_FILE = tmp_path / "state.json"
        try:
            save_state(CLIState(session_id="test", mode="cloud", frame_selector="#nonexistent-frame"))

            await _apply_cli_frame_state(browser_page)

            # Frame state should be cleared since the selector doesn't exist
            assert browser_page._working_frame is None
            reloaded = load_state()
            assert reloaded is not None
            assert reloaded.frame_selector is None
            assert reloaded.frame_name is None
            assert reloaded.frame_index is None
        finally:
            state_mod.STATE_FILE = orig_file

    @pytest.mark.asyncio
    async def test_cli_fill_inside_iframe_after_state_restore(self, browser_page: _TestPage, tmp_path: Any) -> None:
        """Full round-trip: save frame state → fresh page → restore → fill inside iframe."""
        import skyvern.cli.commands._state as state_mod
        from skyvern.cli.commands._state import CLIState, save_state
        from skyvern.cli.commands.browser import _apply_cli_frame_state

        orig_file = state_mod.STATE_FILE
        state_mod.STATE_FILE = tmp_path / "state.json"
        try:
            save_state(CLIState(session_id="test", mode="cloud", frame_selector="#payment-frame"))

            await _apply_cli_frame_state(browser_page)

            # Now do what a CLI `type` command would do after _apply_cli_frame_state
            await browser_page._locator_scope.locator("#card-number").fill("4242424242424242")
            value = await browser_page._locator_scope.locator("#card-number").input_value()
            assert value == "4242424242424242"

            # Main page input should be untouched
            main_value = await browser_page.page.locator("#main-input").input_value()
            assert main_value == ""
        finally:
            state_mod.STATE_FILE = orig_file
