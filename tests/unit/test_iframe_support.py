"""Unit tests for iframe support."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.browser_ops import do_frame_list, do_frame_main, do_frame_switch

# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------


class TestMCPFrameTools:
    @pytest.mark.asyncio
    async def test_frame_switch_invalid_params_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools import browser as mcp_browser

        get_page = AsyncMock(side_effect=AssertionError("get_page should not be called"))
        monkeypatch.setattr(mcp_browser, "get_page", get_page)

        result = await mcp_browser.skyvern_frame_switch()
        assert result["ok"] is False
        assert result["error"]["code"] == mcp_browser.ErrorCode.INVALID_INPUT
        get_page.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_frame_switch_multiple_params_preflight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools import browser as mcp_browser

        get_page = AsyncMock(side_effect=AssertionError("get_page should not be called"))
        monkeypatch.setattr(mcp_browser, "get_page", get_page)

        result = await mcp_browser.skyvern_frame_switch(selector="#x", name="y")
        assert result["ok"] is False
        get_page.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_frame_list_no_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools import browser as mcp_browser
        from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))

        result = await mcp_browser.skyvern_frame_list()
        assert result["ok"] is False
        assert result["error"]["code"] == mcp_browser.ErrorCode.NO_ACTIVE_BROWSER

    @pytest.mark.asyncio
    async def test_frame_main_no_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.mcp_tools import browser as mcp_browser
        from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))

        result = await mcp_browser.skyvern_frame_main()
        assert result["ok"] is False
        assert result["error"]["code"] == mcp_browser.ErrorCode.NO_ACTIVE_BROWSER

    @pytest.mark.asyncio
    async def test_navigate_clears_working_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """skyvern_navigate must clear _working_frame to prevent stale frame references."""
        from skyvern.cli.core.session_manager import SessionState
        from skyvern.cli.mcp_tools import browser as mcp_browser

        fake_page = MagicMock()
        fake_page.goto = AsyncMock()
        fake_page.url = "https://example.com/new"
        fake_page.title = AsyncMock(return_value="New Page")
        fake_ctx = MagicMock()
        fake_ctx.mode = "local"

        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(fake_page, fake_ctx)))

        # Pre-set a working frame on the session state
        state = SessionState()
        state._working_frame = MagicMock()  # simulates an active iframe
        monkeypatch.setattr(mcp_browser, "get_current_session", lambda: state)

        result = await mcp_browser.skyvern_navigate(url="https://example.com/new")
        assert result["ok"] is True
        assert state._working_frame is None

    @pytest.mark.asyncio
    async def test_navigate_clears_frame_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Frame state must be cleared even when navigation fails (partial load may destroy iframes)."""
        from skyvern.cli.core.session_manager import SessionState
        from skyvern.cli.mcp_tools import browser as mcp_browser

        fake_page = MagicMock()
        fake_page.goto = AsyncMock(side_effect=TimeoutError("Navigation timeout"))
        fake_ctx = MagicMock()
        fake_ctx.mode = "local"

        monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(return_value=(fake_page, fake_ctx)))

        state = SessionState()
        state._working_frame = MagicMock()
        monkeypatch.setattr(mcp_browser, "get_current_session", lambda: state)

        result = await mcp_browser.skyvern_navigate(url="https://example.com/timeout")
        assert result["ok"] is False
        assert state._working_frame is None  # cleared despite failure

    @pytest.mark.asyncio
    async def test_tab_switch_clears_working_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Switching tabs must clear _working_frame to prevent stale cross-tab frame references."""
        import skyvern.cli.core.session_manager as sm_mod
        from skyvern.cli.core.session_manager import SessionState
        from skyvern.cli.mcp_tools import tabs as mcp_tabs

        fake_page = MagicMock()
        fake_page.is_closed = MagicMock(return_value=False)
        fake_ctx = MagicMock()
        fake_ctx.mode = "local"
        monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(fake_page, fake_ctx)))
        monkeypatch.setattr(sm_mod, "is_stateless_http_mode", lambda: False)

        target_page = MagicMock()
        target_page.is_closed = MagicMock(return_value=False)
        target_page.url = "https://other-tab.com"
        target_page.title = AsyncMock(return_value="Other Tab")
        target_page.bring_to_front = AsyncMock()

        state = SessionState()
        state.browser = MagicMock()
        state.browser._browser_context.pages = [fake_page, target_page]
        state._working_frame = MagicMock()  # stale frame from previous tab
        monkeypatch.setattr(mcp_tabs, "get_current_session", lambda: state)

        result = await mcp_tabs.skyvern_tab_switch(index=1)
        assert result["ok"] is True
        assert state._working_frame is None

    @pytest.mark.asyncio
    async def test_tab_new_clears_working_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Opening a new tab must clear _working_frame."""
        from skyvern.cli.core.session_manager import SessionState
        from skyvern.cli.mcp_tools import tabs as mcp_tabs

        fake_page = MagicMock()
        fake_ctx = MagicMock()
        fake_ctx.mode = "local"
        monkeypatch.setattr(mcp_tabs, "get_page", AsyncMock(return_value=(fake_page, fake_ctx)))

        new_page = MagicMock()
        new_page.url = "about:blank"
        new_page.title = AsyncMock(return_value="")

        state = SessionState()
        state.browser = MagicMock()
        state.browser._browser_context.new_page = AsyncMock(return_value=new_page)
        state.browser._browser_context.pages = [fake_page, new_page]
        state._working_frame = MagicMock()  # stale frame
        monkeypatch.setattr(mcp_tabs, "get_current_session", lambda: state)

        result = await mcp_tabs.skyvern_tab_new()
        assert result["ok"] is True
        assert state._working_frame is None


# ---------------------------------------------------------------------------
# CLIState frame persistence tests
# ---------------------------------------------------------------------------


class TestCLIStateFrame:
    def test_frame_fields_default_none(self) -> None:
        from skyvern.cli.commands._state import CLIState

        state = CLIState()
        assert state.frame_selector is None
        assert state.frame_name is None
        assert state.frame_index is None

    def test_frame_fields_roundtrip(self, tmp_path) -> None:
        import skyvern.cli.commands._state as state_mod
        from skyvern.cli.commands._state import CLIState, load_state, save_state

        # Point to temp dir to avoid polluting real state
        original_dir = state_mod.STATE_DIR
        original_file = state_mod.STATE_FILE
        state_mod.STATE_DIR = tmp_path
        state_mod.STATE_FILE = tmp_path / "state.json"
        try:
            state = CLIState(
                session_id="pbs_123",
                mode="cloud",
                frame_selector="#payment-frame",
            )
            save_state(state)
            loaded = load_state()
            assert loaded is not None
            assert loaded.frame_selector == "#payment-frame"
            assert loaded.frame_name is None
            assert loaded.frame_index is None
        finally:
            state_mod.STATE_DIR = original_dir
            state_mod.STATE_FILE = original_file


# ---------------------------------------------------------------------------
# browser_ops tests
# ---------------------------------------------------------------------------


class TestBrowserOps:
    @pytest.mark.asyncio
    async def test_do_frame_switch_delegates(self) -> None:
        page = MagicMock()
        page.frame_switch = AsyncMock(return_value={"name": "pay", "url": "https://pay.com"})

        result = await do_frame_switch(page, selector="#iframe")
        page.frame_switch.assert_awaited_once_with(selector="#iframe", name=None, index=None)
        assert result.name == "pay"

    def test_do_frame_main_delegates(self) -> None:
        page = MagicMock()
        page.frame_main = MagicMock(return_value={"status": "switched_to_main_frame"})

        do_frame_main(page)
        page.frame_main.assert_called_once()

    @pytest.mark.asyncio
    async def test_do_frame_list_delegates(self) -> None:
        page = MagicMock()
        page.frame_list = AsyncMock(
            return_value=[
                {"index": 0, "name": "", "url": "https://example.com", "is_main": True},
                {"index": 1, "name": "embed", "url": "https://embed.com", "is_main": False},
            ]
        )

        result = await do_frame_list(page)
        assert len(result) == 2
        assert result[0].is_main is True
        assert result[1].name == "embed"


class TestSessionFramePropagation:
    @pytest.mark.asyncio
    async def test_get_page_propagates_detached_working_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.core import session_manager
        from skyvern.cli.core.result import BrowserContext
        from skyvern.cli.core.session_manager import SessionState
        from skyvern.cli.mcp_tools import inspection

        frame = MagicMock()
        frame.is_detached.return_value = True
        wrapper = MagicMock()
        browser = MagicMock()
        browser.get_working_page = AsyncMock(return_value=wrapper)
        browser._browser_context.pages = []
        browser._browser_context.on = MagicMock()
        ctx = BrowserContext(mode="local")
        state = SessionState(browser=browser, context=ctx)
        state._working_frame = frame
        monkeypatch.setattr(session_manager, "resolve_browser", AsyncMock(return_value=(browser, ctx)))
        monkeypatch.setattr(session_manager, "get_current_session", lambda: state)
        monkeypatch.setattr(inspection, "ensure_hooks_on_all_pages", MagicMock())

        page, _ = await session_manager.get_page()

        assert state._working_frame is frame
        assert page._working_frame is frame
