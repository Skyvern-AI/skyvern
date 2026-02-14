"""
Tests for run_task() prompt parameterization in cached script generation.

When generating cached scripts, block-level prompts (run_task, navigate, etc.) should
replace literal parameter values with f-string references to context.parameters[...],
so that re-runs with different input values don't leak PII from prior runs.
"""

from typing import Any

import libcst as cst

from skyvern.core.script_generations.generate_script import (
    _build_parameterized_prompt_cst,
    _build_run_task_statement,
    _escape_for_fstring_text,
)

# ---------------------------------------------------------------------------
# _escape_for_fstring_text
# ---------------------------------------------------------------------------


class TestEscapeForFstringText:
    def test_escapes_single_braces(self) -> None:
        assert _escape_for_fstring_text("{hello}") == "{{hello}}"

    def test_escapes_jinja2_double_braces(self) -> None:
        # Jinja2 {{param}} → {{{{param}}}} in an f-string text node
        assert _escape_for_fstring_text("{{param}}") == "{{{{param}}}}"

    def test_leaves_plain_text_unchanged(self) -> None:
        assert _escape_for_fstring_text("no braces here") == "no braces here"

    def test_mixed_content(self) -> None:
        text = "Name: {first} and {{last}}"
        assert _escape_for_fstring_text(text) == "Name: {{first}} and {{{{last}}}}"

    def test_empty_string(self) -> None:
        assert _escape_for_fstring_text("") == ""


# ---------------------------------------------------------------------------
# _build_parameterized_prompt_cst — brace escaping
# ---------------------------------------------------------------------------


class TestBuildParameterizedPromptCstBraces:
    """Tests that Jinja2 templates / braces in the prompt text survive parameterization."""

    def test_jinja2_templates_preserved(self) -> None:
        """Prompt with {{param}} Jinja2 template + PII should produce a valid f-string."""
        prompt = "Fill in First Name: MASOOD for {{firstName}}"
        value_to_param = {"MASOOD": "firstName"}

        result = _build_parameterized_prompt_cst(prompt, value_to_param)
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)

        # PII should be replaced
        assert "MASOOD" not in code
        assert "context.parameters" in code
        # The Jinja2 template should survive as literal braces
        assert "firstName" in code
        # Should be compilable Python
        compile(code, "<test>", "eval")

    def test_braces_in_non_match_segments(self) -> None:
        """Braces in text segments that aren't PII should be escaped."""
        prompt = "Look for {item} with ID 542-641-668"
        value_to_param = {"542-641-668": "patient_id"}

        result = _build_parameterized_prompt_cst(prompt, value_to_param)
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)

        assert "542-641-668" not in code
        assert "patient_id" in code
        # Should be compilable (braces are escaped)
        compile(code, "<test>", "eval")


# ---------------------------------------------------------------------------
# _build_parameterized_prompt_cst — triple-quote support
# ---------------------------------------------------------------------------


class TestBuildParameterizedPromptCstTripleQuote:
    """Tests that multiline prompts use triple-quote f-strings."""

    def test_multiline_prompt_uses_triple_quote(self) -> None:
        prompt = "Fill in the form:\nFirst Name: MASOOD\nLast Name: SABIR"
        value_to_param = {"MASOOD": "firstName", "SABIR": "lastName"}

        result = _build_parameterized_prompt_cst(prompt, value_to_param)
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)

        assert code.startswith('f"""')
        assert code.endswith('"""')
        assert "MASOOD" not in code
        assert "SABIR" not in code
        compile(code, "<test>", "eval")

    def test_single_line_prompt_uses_regular_quote(self) -> None:
        prompt = "Find patient MASOOD SABIR"
        value_to_param = {"MASOOD SABIR": "fullName"}

        result = _build_parameterized_prompt_cst(prompt, value_to_param)
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)

        assert code.startswith('f"')
        assert not code.startswith('f"""')

    def test_prompt_with_quotes_uses_triple_quote(self) -> None:
        prompt = """Click the "Submit" button for MASOOD"""
        value_to_param = {"MASOOD": "firstName"}

        result = _build_parameterized_prompt_cst(prompt, value_to_param)
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)

        assert code.startswith('f"""')


# ---------------------------------------------------------------------------
# End-to-end: _build_run_task_statement with value_to_param
# ---------------------------------------------------------------------------


def _make_block(
    navigation_goal: str,
    block_type: str = "task",
    parameters: list[dict[str, str]] | None = None,
    label: str = "test_block",
    url: str = "",
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "block_type": block_type,
        "navigation_goal": navigation_goal,
        "label": label,
    }
    if parameters:
        block["parameters"] = parameters
    if url:
        block["url"] = url
    return block


def _render_stmt(stmt: cst.SimpleStatementLine) -> str:
    return cst.Module(body=[stmt]).code


class TestBuildRunTaskStatementParameterization:
    """End-to-end tests: _build_run_task_statement with value_to_param."""

    def test_pii_in_navigation_goal_is_parameterized(self) -> None:
        """PII in Steps section should be replaced with context.parameters refs."""
        block = _make_block(
            navigation_goal=("Navigate to the form.\nSteps:\nFill in (First Name: MASOOD, Last Name: SABIR)"),
        )
        value_to_param = {"MASOOD": "firstName", "SABIR": "lastName"}

        stmt = _build_run_task_statement("test_block", block, value_to_param=value_to_param)
        code = _render_stmt(stmt)

        assert "MASOOD" not in code
        assert "SABIR" not in code
        assert "context.parameters" in code
        assert "firstName" in code
        assert "lastName" in code

    def test_jinja2_templates_in_navigation_payload_preserved(self) -> None:
        """Jinja2 {{param}} templates appended by navigation_payload should survive."""
        block = _make_block(
            navigation_goal="Fill in First Name: MASOOD",
            parameters=[{"key": "firstName"}, {"key": "lastName"}],
        )
        value_to_param = {"MASOOD": "firstName"}

        stmt = _build_run_task_statement("test_block", block, value_to_param=value_to_param)
        code = _render_stmt(stmt)

        # PII replaced
        assert "MASOOD" not in code
        assert "context.parameters" in code
        # The code should be syntactically valid Python (wrap in async def since it has await)
        compile(f"async def _test():\n    {code}", "<test>", "exec")

    def test_no_parameterization_without_value_to_param(self) -> None:
        """Without value_to_param, prompt should be a plain string literal."""
        block = _make_block(navigation_goal="Fill in First Name: MASOOD")

        stmt = _build_run_task_statement("test_block", block, value_to_param=None)
        code = _render_stmt(stmt)

        assert "MASOOD" in code
        assert "context.parameters" not in code

    def test_no_parameterization_when_no_match(self) -> None:
        """When value_to_param doesn't match anything, prompt is a plain literal."""
        block = _make_block(navigation_goal="Navigate to the dashboard")
        value_to_param = {"MASOOD": "firstName"}

        stmt = _build_run_task_statement("test_block", block, value_to_param=value_to_param)
        code = _render_stmt(stmt)

        assert "Navigate to the dashboard" in code
        assert "context.parameters" not in code

    def test_task_v2_block_parameterized(self) -> None:
        """task_v2 blocks use 'prompt' key instead of 'navigation_goal'."""
        block: dict[str, Any] = {
            "block_type": "task_v2",
            "prompt": "Fill in the form for MASOOD SABIR",
            "label": "test_v2",
        }
        value_to_param = {"MASOOD SABIR": "fullName"}

        stmt = _build_run_task_statement("test_v2", block, value_to_param=value_to_param)
        code = _render_stmt(stmt)

        assert "MASOOD SABIR" not in code
        assert "context.parameters" in code
        assert "fullName" in code
