"""Tests for static script infrastructure: mode='direct', BLOCK_MAP resolution, nativeSel."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# nativeSel (JavaScript helper) — test via a Python reimplementation
# ---------------------------------------------------------------------------


def _native_sel(sel: str | None) -> str | None:
    """Python reimplementation of nativeSel() from the platform form fields JS extension."""
    import re

    if not sel:
        return None
    result = re.sub(r":visible", "", sel)
    result = re.sub(r':has-text\("[^"]*"\)', "", result)
    return result.strip() or None


class TestNativeSel:
    def test_strips_visible(self) -> None:
        assert _native_sel('input[name="foo"]:visible') == 'input[name="foo"]'

    def test_strips_has_text(self) -> None:
        assert _native_sel('label:has-text("Submit") input:visible') == "label input"

    def test_handles_parens_in_has_text(self) -> None:
        assert _native_sel(':has-text("Click (here)")') is None  # fully stripped

    def test_none_input(self) -> None:
        assert _native_sel(None) is None

    def test_empty_string(self) -> None:
        assert _native_sel("") is None

    def test_no_pseudo_selectors(self) -> None:
        assert _native_sel('input[type="text"]') == 'input[type="text"]'

    def test_multiple_visible(self) -> None:
        assert _native_sel('[role="option"]:visible, .item:visible') == '[role="option"], .item'


# ---------------------------------------------------------------------------
# BLOCK_MAP resolution logic
# ---------------------------------------------------------------------------


class TestBlockMapResolution:
    """Test the BLOCK_MAP matching logic from ensure_static_script."""

    def _resolve(
        self,
        blocks: list[tuple[str, str]],
        module_attrs: list[str],
        block_map: dict[str, str],
    ) -> dict[str, str]:
        """Simulate the BLOCK_MAP resolution loop from agent_functions.py.

        Args:
            blocks: List of (label, block_type) tuples
            module_attrs: Function names available on the module
            block_map: The BLOCK_MAP dict

        Returns:
            Dict mapping block_label -> cache_key
        """
        result: dict[str, str] = {}
        module = types.ModuleType("test_module")
        for attr in module_attrs:
            setattr(module, attr, lambda: None)

        for label, block_type in blocks:
            if not label:
                continue
            if hasattr(module, label):
                cache_key = label
            else:
                cache_key = block_map.get(block_type, None)
            if not cache_key or not hasattr(module, cache_key):
                continue
            result[label] = cache_key
        return result

    def test_exact_match(self) -> None:
        result = self._resolve(
            blocks=[("create_account", "login")],
            module_attrs=["create_account"],
            block_map={},
        )
        assert result == {"create_account": "create_account"}

    def test_block_map_match(self) -> None:
        result = self._resolve(
            blocks=[("register_or_login", "login"), ("fill_and_submit", "navigation")],
            module_attrs=["create_account", "fill_application"],
            block_map={"login": "create_account", "navigation": "fill_application"},
        )
        assert result == {
            "register_or_login": "create_account",
            "fill_and_submit": "fill_application",
        }

    def test_wait_block_skipped(self) -> None:
        result = self._resolve(
            blocks=[("register_or_login", "login"), ("wait_block", "wait"), ("fill_app", "navigation")],
            module_attrs=["create_account", "fill_application"],
            block_map={"login": "create_account", "navigation": "fill_application"},
        )
        assert "wait_block" not in result
        assert len(result) == 2

    def test_no_label_skipped(self) -> None:
        result = self._resolve(
            blocks=[("", "login")],
            module_attrs=["create_account"],
            block_map={"login": "create_account"},
        )
        assert result == {}

    def test_block_map_typo_skipped(self) -> None:
        """BLOCK_MAP points to non-existent function — should skip, not crash."""
        result = self._resolve(
            blocks=[("my_login", "login")],
            module_attrs=["create_account"],
            block_map={"login": "nonexistent_function"},
        )
        assert result == {}

    def test_block_map_zero_matches_when_empty(self) -> None:
        result = self._resolve(
            blocks=[("block1", "custom_type")],
            module_attrs=["some_func"],
            block_map={"login": "create_account"},
        )
        assert result == {}

    def test_three_blocks_with_mixed_matching(self) -> None:
        """Workflow with 3 blocks: exact match, BLOCK_MAP, and unmapped."""
        result = self._resolve(
            blocks=[
                ("create_account", "login"),
                ("fill_form", "navigation"),
                ("unknown_step", "extraction"),
            ],
            module_attrs=["create_account", "fill_application"],
            block_map={"navigation": "fill_application"},
        )
        assert result == {
            "create_account": "create_account",
            "fill_form": "fill_application",
        }
        assert "unknown_step" not in result


# ---------------------------------------------------------------------------
# mode="direct" on click() and fill()
# ---------------------------------------------------------------------------


class TestModeDirectClick:
    """Test that mode='direct' short-circuits before backward compat / validation."""

    @pytest.mark.asyncio
    async def test_direct_click_requires_selector(self) -> None:
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        page = MagicMock()
        skyvern_page = SkyvernPage.__new__(SkyvernPage)
        skyvern_page.page = page

        with pytest.raises(ValueError, match="mode='direct' requires a selector"):
            await skyvern_page.click(mode="direct")

    @pytest.mark.asyncio
    async def test_direct_fill_requires_selector(self) -> None:
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        page = MagicMock()
        skyvern_page = SkyvernPage.__new__(SkyvernPage)
        skyvern_page.page = page

        with pytest.raises(ValueError, match="mode='direct' requires a selector"):
            await skyvern_page.fill(value="test", mode="direct")

    @pytest.mark.asyncio
    async def test_direct_fill_requires_value(self) -> None:
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        page = MagicMock()
        skyvern_page = SkyvernPage.__new__(SkyvernPage)
        skyvern_page.page = page

        with pytest.raises(ValueError, match="mode='direct' requires a value"):
            await skyvern_page.fill(selector="input", mode="direct")


# ---------------------------------------------------------------------------
# Consecutive validation failure limit
# ---------------------------------------------------------------------------


class TestValidationFailureLimit:
    def test_counter_logic(self) -> None:
        """Verify the consecutive failure counter logic."""
        consecutive = 0
        max_failures = 3
        pages = []

        for page_num in range(10):
            has_errors = page_num < 5  # first 5 pages have errors
            if has_errors:
                consecutive += 1
                if consecutive >= max_failures:
                    pages.append(f"p{page_num}:STOPPED")
                    break
                pages.append(f"p{page_num}:retry")
            else:
                consecutive = 0
                pages.append(f"p{page_num}:ok")

        # Should stop at page 2 (3rd consecutive failure)
        assert len(pages) == 3
        assert pages[-1] == "p2:STOPPED"
