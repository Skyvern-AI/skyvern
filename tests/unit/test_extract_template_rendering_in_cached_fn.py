"""Tests for Jinja template rendering in cached extraction function bodies.

Validates that extraction prompts containing workflow parameter references
(e.g. {{downloaded_files}}) are wrapped in skyvern.render_template() so
they resolve at runtime instead of being passed as literal text to the LLM.
"""

import libcst as cst

from skyvern.core.script_generations.generate_script import _build_block_fn


def _make_extraction_block(prompt: str) -> dict:
    return {
        "label": "extract_documents",
        "block_type": "extraction",
        "data_extraction_goal": prompt,
    }


def _make_extract_action(prompt: str) -> dict:
    return {
        "action_type": "extract",
        "data_extraction_goal": prompt,
        "data": {"documents": []},
    }


class TestExtractTemplateRendering:
    def test_prompt_with_template_is_wrapped_in_render_template(self) -> None:
        """Extract prompts containing {{ }} should be wrapped in skyvern.render_template()."""
        prompt = "Extract documents not in {{downloaded_files}}"
        block = _make_extraction_block(prompt)
        actions = [_make_extract_action(prompt)]

        fn_def = _build_block_fn(block, actions)
        code = cst.Module(body=[fn_def]).code

        assert "render_template" in code
        assert "{{downloaded_files}}" in code

    def test_prompt_without_template_is_literal(self) -> None:
        """Extract prompts without {{ }} should be plain string literals (no render_template)."""
        prompt = "Extract all document titles from the page"
        block = _make_extraction_block(prompt)
        actions = [_make_extract_action(prompt)]

        fn_def = _build_block_fn(block, actions)
        code = cst.Module(body=[fn_def]).code

        assert "render_template" not in code
        assert "Extract all document titles" in code

    def test_prompt_with_multiple_templates(self) -> None:
        """Prompts with multiple template variables should all be rendered."""
        prompt = "Merge {{downloaded_files}} with {{workflow_run_summary}}"
        block = _make_extraction_block(prompt)
        actions = [_make_extract_action(prompt)]

        fn_def = _build_block_fn(block, actions)
        code = cst.Module(body=[fn_def]).code

        assert "render_template" in code
        assert "{{downloaded_files}}" in code
        assert "{{workflow_run_summary}}" in code
