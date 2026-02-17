"""
Tests for click prompt parameterization in cached script generation.

When generating cached scripts, click action prompts (intention/reasoning) should
replace literal parameter values with f-string references to context.parameters[...],
so that re-runs with different values produce correct behavior.
"""

from typing import Any

import libcst as cst

from skyvern.core.script_generations.generate_script import (
    MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB,
    _action_to_stmt,
    _build_parameterized_prompt_cst,
    _build_value_to_param_lookup,
)
from skyvern.webeye.actions.actions import ActionType


def _make_action(
    action_type: str,
    field_name: str | None = None,
    text: str = "",
    option: str = "",
    file_url: str = "",
) -> dict[str, Any]:
    action: dict[str, Any] = {"action_type": action_type}
    if field_name:
        action["field_name"] = field_name
    if text:
        action["text"] = text
    if option:
        action["option"] = option
    if file_url:
        action["file_url"] = file_url
    return action


# ---------------------------------------------------------------------------
# _build_value_to_param_lookup
# ---------------------------------------------------------------------------


class TestBuildValueToParamLookup:
    def test_collects_input_text_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="patient_id", text="542-641-668"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {"542-641-668": "patient_id"}

    def test_collects_select_option_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.SELECT_OPTION, field_name="state", option="California"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {"California": "state"}

    def test_collects_upload_file_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(
                    ActionType.UPLOAD_FILE,
                    field_name="document",
                    file_url="https://example.com/report.pdf",
                ),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {"https://example.com/report.pdf": "document"}

    def test_skips_actions_without_field_name(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, text="some value without field name"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {}

    def test_skips_short_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="flag", text="No"),
                _make_action(ActionType.INPUT_TEXT, field_name="code", text="CA"),
                _make_action(ActionType.INPUT_TEXT, field_name="num", text="1"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {}

    def test_boundary_value_at_min_length(self) -> None:
        """Values at exactly MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB should be included."""
        value = "x" * MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="field", text=value),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert value in lookup

    def test_sorted_by_descending_length(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="short_field", text="abcd"),
                _make_action(ActionType.INPUT_TEXT, field_name="long_field", text="abcdefghij"),
                _make_action(ActionType.INPUT_TEXT, field_name="mid_field", text="abcdef"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        keys = list(lookup.keys())
        assert keys == ["abcdefghij", "abcdef", "abcd"]

    def test_first_writer_wins_on_duplicate_values(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="first_field", text="same-value"),
                _make_action(ActionType.INPUT_TEXT, field_name="second_field", text="same-value"),
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup["same-value"] == "first_field"

    def test_skips_click_actions(self) -> None:
        actions_by_task = {
            "task-1": [
                {"action_type": ActionType.CLICK, "field_name": "click_field", "text": "some text"},
            ]
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {}

    def test_multiple_tasks(self) -> None:
        actions_by_task = {
            "task-1": [
                _make_action(ActionType.INPUT_TEXT, field_name="patient_id", text="542-641-668"),
            ],
            "task-2": [
                _make_action(ActionType.INPUT_TEXT, field_name="doctor_name", text="Dr. Smith"),
            ],
        }
        lookup = _build_value_to_param_lookup(actions_by_task)
        assert lookup == {"542-641-668": "patient_id", "Dr. Smith": "doctor_name"}

    def test_empty_actions(self) -> None:
        lookup = _build_value_to_param_lookup({})
        assert lookup == {}


# ---------------------------------------------------------------------------
# _build_parameterized_prompt_cst
# ---------------------------------------------------------------------------


class TestBuildParameterizedPromptCst:
    def test_returns_none_when_no_matches(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Click the submit button",
            {"542-641-668": "patient_id"},
        )
        assert result is None

    def test_single_substitution(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Which card corresponds to the referral for ID 542-641-668?",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        assert isinstance(result, cst.FormattedString)
        code = cst.Module(body=[]).code_for_node(result)
        assert "context.parameters" in code
        assert "patient_id" in code
        assert "542-641-668" not in code

    def test_multiple_substitutions(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Find patient 542-641-668 with doctor Dr. Smith",
            {"542-641-668": "patient_id", "Dr. Smith": "doctor_name"},
        )
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)
        assert "patient_id" in code
        assert "doctor_name" in code
        assert "542-641-668" not in code
        assert "Dr. Smith" not in code

    def test_substitution_at_start(self) -> None:
        result = _build_parameterized_prompt_cst(
            "542-641-668 is the patient ID to search for",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        parts = result.parts
        # First part should be the expression (substitution at start)
        assert isinstance(parts[0], cst.FormattedStringExpression)

    def test_substitution_at_end(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Search for patient 542-641-668",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        parts = result.parts
        # Last part should be the expression (substitution at end)
        assert isinstance(parts[-1], cst.FormattedStringExpression)

    def test_empty_intention(self) -> None:
        result = _build_parameterized_prompt_cst("", {"542-641-668": "patient_id"})
        assert result is None

    def test_empty_lookup(self) -> None:
        result = _build_parameterized_prompt_cst(
            "Which card corresponds to the referral for ID 542-641-668?",
            {},
        )
        assert result is None

    def test_longer_match_preferred_over_shorter(self) -> None:
        """When values overlap, the longer value (sorted first) takes precedence."""
        result = _build_parameterized_prompt_cst(
            "Enter 542-641-668-999 here",
            {
                "542-641-668-999": "full_id",
                "542-641-668": "partial_id",
            },
        )
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)
        assert "full_id" in code
        assert "partial_id" not in code

    def test_generates_valid_fstring_syntax(self) -> None:
        """The generated f-string should be parseable Python."""
        result = _build_parameterized_prompt_cst(
            "Which card area corresponds to ID 542-641-668?",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)
        # Should be a valid f-string — verify it starts with f" or f'
        assert code.startswith("f'") or code.startswith('f"')
        # The full expression should be compilable
        compile(code, "<test>", "eval")

    def test_repeated_value_in_intention(self) -> None:
        """If the same value appears twice, both occurrences should be replaced."""
        result = _build_parameterized_prompt_cst(
            "Compare 542-641-668 with 542-641-668",
            {"542-641-668": "patient_id"},
        )
        assert result is not None
        code = cst.Module(body=[]).code_for_node(result)
        # The literal should not appear at all
        assert "542-641-668" not in code
        # context.parameters should appear twice (once per occurrence)
        assert code.count("context.parameters") == 2


# ---------------------------------------------------------------------------
# Integration: _action_to_stmt with value_to_param
# ---------------------------------------------------------------------------


class TestActionToStmtClickParameterization:
    """End-to-end tests exercising _action_to_stmt for click actions."""

    def _render(self, stmt: cst.BaseStatement) -> str:
        return cst.Module(body=[stmt]).code

    def test_click_prompt_parameterized(self) -> None:
        """Click action with matching value in intention gets an f-string prompt."""
        act: dict[str, Any] = {
            "action_type": "click",
            "xpath": "//div[@class='card']",
            "intention": "Which card corresponds to the referral for ID 542-641-668?",
        }
        task: dict[str, Any] = {}
        value_to_param = {"542-641-668": "patient_id"}

        stmt = _action_to_stmt(act, task, value_to_param=value_to_param)
        code = self._render(stmt)

        assert "context.parameters" in code
        assert "patient_id" in code
        assert "542-641-668" not in code
        # Should be an f-string
        assert "f'" in code or 'f"' in code

    def test_click_prompt_literal_when_no_lookup(self) -> None:
        """Click action without value_to_param produces a plain string prompt."""
        act: dict[str, Any] = {
            "action_type": "click",
            "xpath": "//div[@class='card']",
            "intention": "Click the submit button",
        }
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(act, task, value_to_param=None)
        code = self._render(stmt)

        assert "Click the submit button" in code
        assert "context.parameters" not in code

    def test_click_prompt_literal_when_no_match(self) -> None:
        """Click action with non-matching lookup produces a plain string prompt."""
        act: dict[str, Any] = {
            "action_type": "click",
            "xpath": "//button",
            "intention": "Click the submit button",
        }
        task: dict[str, Any] = {}
        value_to_param = {"542-641-668": "patient_id"}

        stmt = _action_to_stmt(act, task, value_to_param=value_to_param)
        code = self._render(stmt)

        assert "Click the submit button" in code
        assert "context.parameters" not in code

    def test_fill_action_unaffected_by_value_to_param(self) -> None:
        """Fill actions should still use the field_name mechanism, not value_to_param."""
        act: dict[str, Any] = {
            "action_type": "input_text",
            "xpath": "//input[@name='search']",
            "text": "542-641-668",
            "field_name": "patient_id",
        }
        task: dict[str, Any] = {}
        value_to_param = {"542-641-668": "patient_id"}

        stmt = _action_to_stmt(act, task, value_to_param=value_to_param)
        code = self._render(stmt)

        # Should use context.parameters via the field_name mechanism, not f-string
        assert "context.parameters" in code
        assert "patient_id" in code
