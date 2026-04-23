"""Regression test: adaptive caching must never generate page.fill_form().

fill_form() delegates entirely to AI at runtime, defeating the purpose of
caching.  It is only appropriate for ATS static scripts (built via a
separate pipeline).  See PR #10043 and PR #10195.
"""

from unittest.mock import MagicMock, patch

import libcst as cst

from skyvern.core.script_generations.generate_script import _build_block_fn


def _make_form_block(label: str = "fill_application") -> dict:
    return {
        "label": label,
        "block_type": "navigation",
        "url": "https://example.com/apply",
        "navigation_goal": "Fill out the application form with the provided information",
    }


def _make_input_action(field_name: str, value: str = "test") -> dict:
    return {
        "action_type": "input_text",
        "xpath": f"/html/body/form/input[@name='{field_name}']",
        "element_id": f"elem_{field_name}",
        "reasoning": f"Fill the {field_name} field",
        "text": value,
    }


def _make_select_action(field_name: str) -> dict:
    return {
        "action_type": "select_option",
        "xpath": f"/html/body/form/select[@name='{field_name}']",
        "element_id": f"elem_{field_name}",
        "reasoning": f"Select {field_name}",
        "option": {"label": "Option A", "value": "a"},
    }


class TestNoFillFormInAdaptiveCodegen:
    def test_block_with_many_form_actions_does_not_generate_fill_form(self) -> None:
        """A block with 4+ input/select actions must generate explicit
        page.fill/page.click calls, NOT page.fill_form()."""
        block = _make_form_block()
        actions = [
            _make_input_action("first_name", "John"),
            _make_input_action("last_name", "Doe"),
            _make_input_action("email", "john@example.com"),
            _make_input_action("phone", "555-1234"),
            _make_select_action("country"),
        ]

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.AGENT_FUNCTION.build_ats_pipeline_block_fn = MagicMock(return_value=None)
            func_def = _build_block_fn(
                block=block,
                actions=actions,
                use_semantic_selectors=True,
            )

        code = cst.parse_module("").code_for_node(func_def)
        assert "fill_form" not in code, (
            "Adaptive caching generated fill_form() for a non-ATS block. "
            "fill_form delegates to AI at runtime and defeats caching."
        )
        # Should have explicit page.fill or page.click calls instead
        assert "page.fill(" in code or "page.click(" in code

    def test_select_option_uses_fallback_not_proactive_with_semantic_selectors(self) -> None:
        """When adaptive caching (use_semantic_selectors=True) generates a
        select_option call with a value, it must use ai='fallback' so the
        selector is tried first.  ai='proactive' would route straight to the
        LLM every run, defeating caching for blocks with dropdowns."""
        block = _make_form_block()
        actions = [
            _make_input_action("first_name"),
            _make_input_action("last_name"),
            _make_input_action("email"),
            _make_input_action("phone"),
            _make_select_action("country"),
        ]

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.AGENT_FUNCTION.build_ats_pipeline_block_fn = MagicMock(return_value=None)
            func_def = _build_block_fn(
                block=block,
                actions=actions,
                use_semantic_selectors=True,
            )

        code = cst.parse_module("").code_for_node(func_def)
        # The generated select_option call should use ai='fallback', not 'proactive'.
        # 'proactive' routes straight to ai_select_option() without trying the
        # selector, which makes every run an LLM call.
        assert "page.select_option(" in code
        # Isolate the select_option call's body so this test doesn't accidentally
        # pass on a `page.fill(..., ai='fallback', ...)` earlier in the function.
        select_start = code.index("page.select_option(")
        select_block = code[select_start : code.index(")", select_start) + 1]
        # Normalize whitespace around `=` so `ai = 'fallback'` and `ai='fallback'` both match.
        normalized = select_block.replace(" = ", "=")
        assert "ai='fallback'" in normalized, (
            "select_option with a value must use ai='fallback' under adaptive "
            "caching — 'proactive' defeats the selector path.  Got:\n" + select_block
        )
