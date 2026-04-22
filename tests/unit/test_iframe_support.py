"""Unit tests for iframe support: _locator_scope, frame_switch, frame_main, frame_list."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.browser_ops import do_frame_list, do_frame_main, do_frame_switch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_frame(*, name: str = "", url: str = "about:blank") -> MagicMock:
    frame = MagicMock()
    frame.name = name
    frame.url = url
    frame.locator = MagicMock(return_value=MagicMock())
    return frame


def _make_fake_page(frames: list[MagicMock] | None = None) -> MagicMock:
    """Build a mock Playwright Page with .locator(), .frames, .main_frame, .frame()."""
    page = MagicMock()
    all_frames = frames or [_make_fake_frame(name="main", url="https://example.com")]
    page.frames = all_frames
    page.main_frame = all_frames[0]
    page.locator = MagicMock(return_value=MagicMock())
    page.frame = MagicMock(return_value=None)
    return page


class FakeSkyvernPage:
    """Minimal stand-in for SkyvernPage to test _locator_scope without Playwright."""

    def __init__(self, page: Any, working_frame: Any = None) -> None:
        self.page = page
        self._working_frame = working_frame

    @property
    def _locator_scope(self) -> Any:
        frame = object.__getattribute__(self, "_working_frame")
        if frame is not None:
            return frame
        return object.__getattribute__(self, "page")


class FakeSkyvernBrowserPage(FakeSkyvernPage):
    """Minimal stand-in for SkyvernBrowserPage to test frame methods."""

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
                raise ValueError(f"Frame index {index} out of range (0-{len(frames) - 1}).")
            frame = frames[index]

        self._working_frame = frame
        return {
            "name": frame.name if frame else None,
            "url": frame.url if frame else None,
            "selector": selector,
            "frame_name": name,
            "index": index,
        }

    def frame_main(self) -> dict[str, str]:
        self._working_frame = None
        return {"status": "switched_to_main_frame"}

    async def frame_list(self) -> list[dict[str, Any]]:
        frames = self.page.frames
        return [
            {"index": i, "name": f.name, "url": f.url, "is_main": f == self.page.main_frame}
            for i, f in enumerate(frames)
        ]


# ---------------------------------------------------------------------------
# _locator_scope tests
# ---------------------------------------------------------------------------


class TestLocatorScope:
    def test_returns_page_when_no_frame(self) -> None:
        page = _make_fake_page()
        sp = FakeSkyvernPage(page)
        assert sp._locator_scope is page

    def test_returns_frame_when_set(self) -> None:
        page = _make_fake_page()
        frame = _make_fake_frame(name="iframe1")
        sp = FakeSkyvernPage(page, working_frame=frame)
        assert sp._locator_scope is frame

    def test_returns_page_after_clearing_frame(self) -> None:
        page = _make_fake_page()
        frame = _make_fake_frame()
        sp = FakeSkyvernPage(page, working_frame=frame)
        assert sp._locator_scope is frame
        sp._working_frame = None
        assert sp._locator_scope is page

    def test_locator_call_delegates_to_frame(self) -> None:
        page = _make_fake_page()
        frame = _make_fake_frame()
        sp = FakeSkyvernPage(page, working_frame=frame)
        sp._locator_scope.locator("#btn")
        frame.locator.assert_called_once_with("#btn")
        page.locator.assert_not_called()

    def test_locator_call_delegates_to_page_when_no_frame(self) -> None:
        page = _make_fake_page()
        sp = FakeSkyvernPage(page)
        sp._locator_scope.locator("#btn")
        page.locator.assert_called_once_with("#btn")


# ---------------------------------------------------------------------------
# frame_switch tests
# ---------------------------------------------------------------------------


class TestFrameSwitch:
    @pytest.mark.asyncio
    async def test_switch_by_selector(self) -> None:
        iframe = _make_fake_frame(name="payment", url="https://payment.example.com/v3")
        element_mock = MagicMock()
        element_mock.content_frame = AsyncMock(return_value=iframe)

        page = _make_fake_page()
        page.query_selector = AsyncMock(return_value=element_mock)

        sp = FakeSkyvernBrowserPage(page)
        result = await sp.frame_switch(selector="#payment-frame")

        assert sp._working_frame is iframe
        assert result["name"] == "payment"
        assert result["url"] == "https://payment.example.com/v3"

    @pytest.mark.asyncio
    async def test_switch_by_name(self) -> None:
        iframe = _make_fake_frame(name="checkout", url="https://checkout.com")
        page = _make_fake_page()
        page.frame = MagicMock(return_value=iframe)

        sp = FakeSkyvernBrowserPage(page)
        result = await sp.frame_switch(name="checkout")

        assert sp._working_frame is iframe
        assert result["frame_name"] == "checkout"

    @pytest.mark.asyncio
    async def test_switch_by_index(self) -> None:
        main = _make_fake_frame(name="main", url="https://example.com")
        iframe = _make_fake_frame(name="embed", url="https://embed.com")
        page = _make_fake_page([main, iframe])

        sp = FakeSkyvernBrowserPage(page)
        result = await sp.frame_switch(index=1)

        assert sp._working_frame is iframe
        assert result["index"] == 1

    @pytest.mark.asyncio
    async def test_switch_no_params_raises(self) -> None:
        page = _make_fake_page()
        sp = FakeSkyvernBrowserPage(page)
        with pytest.raises(ValueError, match="Exactly one"):
            await sp.frame_switch()

    @pytest.mark.asyncio
    async def test_switch_multiple_params_raises(self) -> None:
        page = _make_fake_page()
        sp = FakeSkyvernBrowserPage(page)
        with pytest.raises(ValueError, match="Exactly one"):
            await sp.frame_switch(selector="#x", name="y")

    @pytest.mark.asyncio
    async def test_switch_selector_not_iframe_raises(self) -> None:
        element_mock = MagicMock()
        element_mock.content_frame = AsyncMock(return_value=None)

        page = _make_fake_page()
        page.query_selector = AsyncMock(return_value=element_mock)

        sp = FakeSkyvernBrowserPage(page)
        with pytest.raises(ValueError, match="did not resolve to an iframe"):
            await sp.frame_switch(selector="#not-iframe")

    @pytest.mark.asyncio
    async def test_switch_name_not_found_raises(self) -> None:
        page = _make_fake_page()
        page.frame = MagicMock(return_value=None)

        sp = FakeSkyvernBrowserPage(page)
        with pytest.raises(ValueError, match="No frame found"):
            await sp.frame_switch(name="nonexistent")

    @pytest.mark.asyncio
    async def test_switch_index_out_of_range_raises(self) -> None:
        page = _make_fake_page([_make_fake_frame()])
        sp = FakeSkyvernBrowserPage(page)
        with pytest.raises(ValueError, match="out of range"):
            await sp.frame_switch(index=5)


# ---------------------------------------------------------------------------
# frame_main tests
# ---------------------------------------------------------------------------


class TestFrameMain:
    def test_clears_working_frame(self) -> None:
        page = _make_fake_page()
        frame = _make_fake_frame()
        sp = FakeSkyvernBrowserPage(page, working_frame=frame)
        assert sp._working_frame is frame
        sp.frame_main()
        assert sp._working_frame is None
        assert sp._locator_scope is page


# ---------------------------------------------------------------------------
# frame_list tests
# ---------------------------------------------------------------------------


class TestFrameList:
    @pytest.mark.asyncio
    async def test_lists_all_frames(self) -> None:
        main = _make_fake_frame(name="", url="https://example.com")
        iframe1 = _make_fake_frame(name="ads", url="https://ads.com")
        iframe2 = _make_fake_frame(name="payment", url="https://payment.example.com")
        page = _make_fake_page([main, iframe1, iframe2])

        sp = FakeSkyvernBrowserPage(page)
        frames = await sp.frame_list()

        assert len(frames) == 3
        assert frames[0]["is_main"] is True
        assert frames[1]["name"] == "ads"
        assert frames[2]["name"] == "payment"


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

    def test_frame_fields_roundtrip(self, tmp_path: Any) -> None:
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
