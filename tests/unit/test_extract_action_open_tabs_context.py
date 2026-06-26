"""Tests for the open-tabs context section and CLOSE_PAGE gate in extract-action prompts."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.prompts import prompt_engine
from skyvern.webeye.utils.page import build_open_tabs_context

_BASE_KWARGS: dict[str, Any] = {
    "navigation_goal": "close the stuck tab and continue",
    "navigation_payload_str": "{}",
    "starting_url": "https://example.test/start",
    "current_url": "https://example.test/form",
    "data_extraction_goal": None,
    "action_history": "[]",
    "error_code_mapping_str": None,
    "local_datetime": "2026-05-14T00:00:00Z",
    "verification_code_check": False,
    "complete_criterion": None,
    "terminate_criterion": None,
    "recent_dialog_messages_str": None,
    "elements": "<html></html>",
}


class TestClosePageGate:
    @pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
    def test_close_page_shown_when_enabled(self, template: str) -> None:
        rendered = prompt_engine.load_prompt(
            template,
            show_close_page_action=True,
            open_tabs_context="Tab 0: https://a.test\nTab 1 [current]: https://b.test",
            **_BASE_KWARGS,
        )
        assert '"CLOSE_PAGE"' in rendered
        assert "closes the current tab" in rendered.lower()
        # close-by-index is documented even when SWITCH_TAB is unavailable
        assert "tab_index" in rendered

    @pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
    def test_no_close_page_without_tab_context(self, template: str) -> None:
        """Magic-link-only scenario: CLOSE_PAGE should NOT appear without tab context.
        Magic-link auto-close is handled by the post-step fallback, not by the prompt."""
        rendered = prompt_engine.load_prompt(
            template,
            show_close_page_action=False,
            open_tabs_context=None,
            **_BASE_KWARGS,
        )
        assert '"CLOSE_PAGE"' not in rendered

    @pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
    def test_no_close_page_when_disabled(self, template: str) -> None:
        rendered = prompt_engine.load_prompt(
            template,
            show_close_page_action=False,
            open_tabs_context=None,
            **_BASE_KWARGS,
        )
        assert '"CLOSE_PAGE"' not in rendered


class TestOpenTabsContextSection:
    @pytest.mark.parametrize("template", ["extract-action", "extract-action-dynamic"])
    def test_renders_tab_listing_when_context_present(self, template: str) -> None:
        tab_ctx = "Tab 0: https://main.test (Main Page)\nTab 1 [current]: https://pdf.test/viewer (PDF Viewer)"
        rendered = prompt_engine.load_prompt(
            template,
            show_close_page_action=True,
            open_tabs_context=tab_ctx,
            **_BASE_KWARGS,
        )
        assert "Open browser tabs" in rendered
        assert "tab_index" in rendered
        assert "[current]" in rendered
        assert "https://main.test" in rendered
        assert "https://pdf.test/viewer" in rendered

    @pytest.mark.parametrize("template", ["extract-action", "extract-action-dynamic"])
    def test_omits_tab_listing_when_no_context(self, template: str) -> None:
        rendered = prompt_engine.load_prompt(
            template,
            show_close_page_action=False,
            open_tabs_context=None,
            **_BASE_KWARGS,
        )
        assert "Open browser tabs" not in rendered

    def test_static_template_omits_tab_listing(self) -> None:
        rendered = prompt_engine.load_prompt(
            "extract-action-static",
            show_close_page_action=True,
            open_tabs_context="Tab 0: https://a.test\nTab 1 [current]: https://b.test",
            **_BASE_KWARGS,
        )
        assert "Open browser tabs" not in rendered


class TestCacheVariant:
    def test_cp_tag_present_when_close_page_enabled(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=False,
            show_close_page_action=True,
            complete_criterion=None,
        )
        assert "cp" in result

    def test_cp_tag_absent_when_close_page_disabled(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=False,
            show_close_page_action=False,
            complete_criterion=None,
        )
        assert "cp" not in result

    def test_std_when_no_flags(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=False,
            show_close_page_action=False,
            complete_criterion=None,
        )
        assert result == "std"

    def test_vc_and_cp_combined(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=True,
            show_close_page_action=True,
            complete_criterion=None,
        )
        assert "vc" in result
        assert "cp" in result


class TestNewTabSwitchTabGate:
    @pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
    def test_new_tab_shown_when_enabled(self, template: str) -> None:
        rendered = prompt_engine.load_prompt(
            template,
            show_close_page_action=False,
            show_new_tab_action=True,
            show_switch_tab_action=False,
            open_tabs_context=None,
            **_BASE_KWARGS,
        )
        assert '"NEW_TAB"' in rendered
        assert '"url"' in rendered
        assert '"SWITCH_TAB"' not in rendered

    @pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
    def test_switch_tab_and_tab_index_key_shown_when_enabled(self, template: str) -> None:
        rendered = prompt_engine.load_prompt(
            template,
            show_close_page_action=True,
            show_new_tab_action=True,
            show_switch_tab_action=True,
            open_tabs_context="Tab 0: https://a.test\nTab 1 [current]: https://b.test",
            **_BASE_KWARGS,
        )
        assert '"SWITCH_TAB"' in rendered
        assert '"tab_index"' in rendered

    @pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
    def test_no_tab_actions_when_disabled(self, template: str) -> None:
        rendered = prompt_engine.load_prompt(
            template,
            show_close_page_action=False,
            show_new_tab_action=False,
            show_switch_tab_action=False,
            open_tabs_context=None,
            **_BASE_KWARGS,
        )
        assert '"NEW_TAB"' not in rendered
        assert '"SWITCH_TAB"' not in rendered
        assert '"tab_index"' not in rendered

    def test_switch_tab_changes_open_tabs_framing(self) -> None:
        switch_on = prompt_engine.load_prompt(
            "extract-action-dynamic",
            show_close_page_action=True,
            show_new_tab_action=True,
            show_switch_tab_action=True,
            open_tabs_context="Tab 0: https://a.test\nTab 1 [current]: https://b.test",
            **_BASE_KWARGS,
        )
        assert "use SWITCH_TAB" in switch_on
        assert "by default" not in switch_on

        switch_off = prompt_engine.load_prompt(
            "extract-action-dynamic",
            show_close_page_action=True,
            show_new_tab_action=False,
            show_switch_tab_action=False,
            open_tabs_context="Tab 0: https://a.test\nTab 1 [current]: https://b.test",
            **_BASE_KWARGS,
        )
        assert "use SWITCH_TAB" not in switch_off
        assert "by default" in switch_off


class TestTabCacheVariant:
    def test_nt_and_st_tags_present_when_enabled(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=False,
            show_close_page_action=False,
            complete_criterion=None,
            show_new_tab_action=True,
            show_switch_tab_action=True,
        )
        assert "nt" in result
        assert "st" in result

    def test_tab_tags_absent_by_default(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=False,
            show_close_page_action=False,
            complete_criterion=None,
        )
        assert result == "std"


class TestBuildOpenTabsContext:
    @pytest.mark.asyncio
    async def test_returns_none_when_working_page_is_none(self) -> None:
        browser_state = MagicMock()
        result = await build_open_tabs_context(browser_state, None)
        assert result is None
        browser_state.list_valid_pages.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_for_single_tab(self) -> None:
        page = MagicMock()
        page.url = "https://example.test"
        browser_state = MagicMock()
        browser_state.list_valid_pages = AsyncMock(return_value=[page])
        result = await build_open_tabs_context(browser_state, page)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_formatted_string_for_multiple_tabs(self) -> None:
        page0 = MagicMock()
        page0.url = "https://example.test/main"
        page0.title = AsyncMock(return_value="Main Page")

        page1 = MagicMock()
        page1.url = "https://example.test/viewer"
        page1.title = AsyncMock(return_value="PDF Viewer")

        browser_state = MagicMock()
        browser_state.list_valid_pages = AsyncMock(return_value=[page0, page1])

        result = await build_open_tabs_context(browser_state, page1)
        assert result is not None
        assert "Tab 0: https://example.test/main (Main Page)" in result
        assert "Tab 1 [current]: https://example.test/viewer (PDF Viewer)" in result

    @pytest.mark.asyncio
    async def test_handles_title_failure_gracefully(self) -> None:
        page0 = MagicMock()
        page0.url = "https://example.test/main"
        page0.title = AsyncMock(side_effect=Exception("page crashed"))

        page1 = MagicMock()
        page1.url = "https://example.test/viewer"
        page1.title = AsyncMock(return_value="Viewer")

        browser_state = MagicMock()
        browser_state.list_valid_pages = AsyncMock(return_value=[page0, page1])

        result = await build_open_tabs_context(browser_state, page1)
        assert result is not None
        assert "Tab 0: https://example.test/main" in result
        assert "(Main Page)" not in result
        assert "Tab 1 [current]: https://example.test/viewer (Viewer)" in result

    @pytest.mark.asyncio
    async def test_truncates_long_urls(self) -> None:
        long_url = "https://example.test/" + "a" * 200
        page0 = MagicMock()
        page0.url = long_url
        page0.title = AsyncMock(return_value="")

        page1 = MagicMock()
        page1.url = "https://short.test"
        page1.title = AsyncMock(return_value="")

        browser_state = MagicMock()
        browser_state.list_valid_pages = AsyncMock(return_value=[page0, page1])

        result = await build_open_tabs_context(browser_state, page1)
        assert result is not None
        assert "..." in result
        for line in result.splitlines():
            if "Tab 0" in line:
                url_part = line.split(": ", 1)[1]
                assert url_part.endswith("...")
                assert len(url_part) <= 120

    @pytest.mark.asyncio
    async def test_truncates_long_titles(self) -> None:
        page0 = MagicMock()
        page0.url = "https://example.test"
        page0.title = AsyncMock(return_value="A" * 200)

        page1 = MagicMock()
        page1.url = "https://short.test"
        page1.title = AsyncMock(return_value="Short")

        browser_state = MagicMock()
        browser_state.list_valid_pages = AsyncMock(return_value=[page0, page1])

        result = await build_open_tabs_context(browser_state, page1)
        assert result is not None
        assert "..." in result
        for line in result.splitlines():
            if "Tab 0" in line:
                title_part = line.split("(", 1)[1].rstrip(")")
                assert len(title_part) <= 80
