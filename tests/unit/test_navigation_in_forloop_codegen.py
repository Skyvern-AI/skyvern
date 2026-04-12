"""Tests for loop blocks using dynamic selectors via context.loop_item_selector().

Validates that navigation and download blocks inside for-loops generate
context.loop_item_selector()-based code instead of hardcoded xpaths
from iteration 0's execution trace.
"""

from unittest.mock import MagicMock, patch

import libcst as cst

from skyvern.core.script_generations.generate_script import _build_block_fn
from skyvern.core.script_generations.skyvern_page import RunContext


def _make_navigation_block(label: str = "navigate_to_page") -> dict:
    return {
        "label": label,
        "block_type": "navigation",
        "url": "https://example.com",
        "navigation_goal": "Click on the document link",
    }


def _make_click_action() -> dict:
    return {
        "action_type": "click",
        "xpath": "/html/body/div/a[3]",
        "element_id": "elem_123",
        "reasoning": "Click on target link",
    }


class TestNavigationInForLoopCodegen:
    def test_navigation_in_loop_uses_loop_item_selector(self) -> None:
        """Navigation blocks inside for-loops should use context.loop_item_selector()
        instead of hardcoded xpath from iteration 0."""
        block = _make_navigation_block()
        actions = [_make_click_action()]

        fn_def = _build_block_fn(block, actions, is_in_for_loop=True)
        code = cst.Module(body=[fn_def]).code

        assert "context.loop_item_selector()" in code
        assert "context.prompt" in code
        assert 'ai="fallback"' in code
        # Should NOT contain the hardcoded xpath from the action
        assert "xpath=" not in code

    def test_navigation_outside_loop_uses_hardcoded_selectors(self) -> None:
        """Navigation blocks NOT in a for-loop should still use action-based xpaths."""
        block = _make_navigation_block()
        actions = [_make_click_action()]

        fn_def = _build_block_fn(block, actions, is_in_for_loop=False)
        code = cst.Module(body=[fn_def]).code

        # Should contain the hardcoded xpath from the action trace
        assert "xpath=" in code
        # Should NOT use dynamic selector
        assert "loop_item_selector" not in code

    def test_download_in_loop_uses_loop_item_selector(self) -> None:
        """file_download blocks in loops should also use loop_item_selector()."""
        block = {
            "label": "download_files",
            "block_type": "file_download",
            "url": "https://example.com",
        }
        actions = [_make_click_action()]

        fn_def = _build_block_fn(block, actions, is_in_for_loop=True)
        code = cst.Module(body=[fn_def]).code

        assert "context.loop_item_selector()" in code
        assert "context.prompt" in code

    def test_extraction_in_loop_not_affected(self) -> None:
        """Extraction blocks in loops should NOT use download_selector (they extract data, not click links)."""
        block = {
            "label": "extract_documents",
            "block_type": "extraction",
            "data_extraction_goal": "Extract document titles",
        }
        actions = [
            {
                "action_type": "extract",
                "data_extraction_goal": "Extract document titles",
                "data": {"titles": ["doc1"]},
            }
        ]

        fn_def = _build_block_fn(block, actions, is_in_for_loop=True)
        code = cst.Module(body=[fn_def]).code

        assert "loop_item_selector" not in code


class TestLoopItemSelector:
    """Direct unit tests for RunContext.loop_item_selector()."""

    def test_url_path_without_extension(self) -> None:
        """Relative paths like /reports/some-report-2024 should use href matching."""
        ctx = RunContext(parameters={}, page=MagicMock(), generated_parameters={})
        mock_skyvern_ctx = MagicMock()
        mock_skyvern_ctx.loop_metadata = {"current_value": {"title": "Some Report", "uid": "/reports/some-report-2024"}}
        with patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_mod:
            mock_mod.current.return_value = mock_skyvern_ctx
            result = ctx.loop_item_selector()
        assert result == 'a[href*="some-report-2024"]'

    def test_full_url_with_path(self) -> None:
        """Full URLs should extract the last path segment."""
        ctx = RunContext(parameters={}, page=MagicMock(), generated_parameters={})
        mock_skyvern_ctx = MagicMock()
        mock_skyvern_ctx.loop_metadata = {
            "current_value": {"title": "Report", "url": "https://example.com/pub/annual-report-2025/"}
        }
        with patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_mod:
            mock_mod.current.return_value = mock_skyvern_ctx
            result = ctx.loop_item_selector()
        assert result == 'a[href*="annual-report-2025"]'

    def test_bare_domain_falls_to_text(self) -> None:
        """URLs with no meaningful path should NOT produce href selectors."""
        ctx = RunContext(parameters={}, page=MagicMock(), generated_parameters={})
        mock_skyvern_ctx = MagicMock()
        mock_skyvern_ctx.loop_metadata = {
            "current_value": {"title": "Annual Report 2024", "url": "https://www.example.gov"}
        }
        with patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_mod:
            mock_mod.current.return_value = mock_skyvern_ctx
            result = ctx.loop_item_selector()
        assert result == 'a:has-text("Annual Report 2024")'

    def test_url_with_file_extension(self) -> None:
        """URLs with file extensions should still work (path segment includes extension)."""
        ctx = RunContext(parameters={}, page=MagicMock(), generated_parameters={})
        mock_skyvern_ctx = MagicMock()
        mock_skyvern_ctx.loop_metadata = {
            "current_value": {"title": "Report", "url": "https://example.com/files/report.pdf"}
        }
        with patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_mod:
            mock_mod.current.return_value = mock_skyvern_ctx
            result = ctx.loop_item_selector()
        assert result == 'a[href*="report.pdf"]'

    def test_text_only_no_urls(self) -> None:
        """Plain text values should use text matching."""
        ctx = RunContext(parameters={}, page=MagicMock(), generated_parameters={})
        mock_skyvern_ctx = MagicMock()
        mock_skyvern_ctx.loop_metadata = {"current_value": {"title": "Quarterly Update", "uid": "quarterly"}}
        with patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_mod:
            mock_mod.current.return_value = mock_skyvern_ctx
            result = ctx.loop_item_selector()
        assert result == 'a:has-text("Quarterly Update")'

    def test_percent_encoded_path_kept_as_is(self) -> None:
        """Percent-encoded characters should be preserved — CSS a[href*=...] matches raw attribute values."""
        ctx = RunContext(parameters={}, page=MagicMock(), generated_parameters={})
        mock_skyvern_ctx = MagicMock()
        mock_skyvern_ctx.loop_metadata = {
            "current_value": {"title": "Report", "url": "https://example.com/files/Monthly%20Report%20Issue.pdf"}
        }
        with patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_mod:
            mock_mod.current.return_value = mock_skyvern_ctx
            result = ctx.loop_item_selector()
        assert result == 'a[href*="Monthly%20Report%20Issue.pdf"]'

    def test_short_path_segment_falls_to_text(self) -> None:
        """URLs with short path segments (< 3 chars) should fall through to text matching."""
        ctx = RunContext(parameters={}, page=MagicMock(), generated_parameters={})
        mock_skyvern_ctx = MagicMock()
        mock_skyvern_ctx.loop_metadata = {
            "current_value": {"title": "Some Document", "url": "https://example.com/go/42"}
        }
        with patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_mod:
            mock_mod.current.return_value = mock_skyvern_ctx
            result = ctx.loop_item_selector()
        assert result == 'a:has-text("Some Document")'
