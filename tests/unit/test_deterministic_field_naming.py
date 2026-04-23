"""Tests for the deterministic field-name picker (SKY-8965 Phase 1).

See `skyvern/core/script_generations/deterministic_field_naming.py` for the
three rules under test:
    1. jinja_ref         — `{{ key }}` in unrendered goal references a known key
    2. upstream_schema   — intention text references an upstream schema key
    3. intention_derived — deterministic fallback from the action's intention
"""

from __future__ import annotations

from skyvern.core.script_generations.deterministic_field_naming import (
    extract_jinja_root_names,
    pick_field_name_for_action,
    pick_field_names_for_actions,
    sanitize_intention_to_field_name,
)
from skyvern.webeye.actions.actions import ActionType

# --- sanitize_intention_to_field_name --------------------------------------


def test_sanitize_preserves_valid_identifiers() -> None:
    assert sanitize_intention_to_field_name("worker_name") == "worker_name"


def test_sanitize_lowercases_and_replaces_spaces() -> None:
    assert sanitize_intention_to_field_name("Worker Name") == "worker_name"


def test_sanitize_strips_punctuation() -> None:
    assert sanitize_intention_to_field_name("What's your email?") == "what_s_your_email"


def test_sanitize_collapses_repeats() -> None:
    assert sanitize_intention_to_field_name("foo   bar   baz") == "foo_bar_baz"


def test_sanitize_handles_leading_digit() -> None:
    result = sanitize_intention_to_field_name("123 dollars")
    assert result.startswith("f_") and "123" in result


def test_sanitize_caps_length() -> None:
    long = "a " * 100
    assert len(sanitize_intention_to_field_name(long)) <= 60


def test_sanitize_empty_returns_fallback() -> None:
    assert sanitize_intention_to_field_name("") == "unknown_field"
    assert sanitize_intention_to_field_name("!!!") == "unknown_field"


# --- extract_jinja_root_names ----------------------------------------------


def test_jinja_root_names_simple() -> None:
    assert extract_jinja_root_names("Search for {{ query }}") == {"query"}


def test_jinja_root_names_multiple() -> None:
    names = extract_jinja_root_names("{{ a }} and {{ b }} with {{ a }}")
    assert names == {"a", "b"}


def test_jinja_root_names_attribute_access() -> None:
    # Only root identifier, not the attribute
    assert extract_jinja_root_names("hi {{ user.name }}") == {"user"}


def test_jinja_root_names_with_filters() -> None:
    assert extract_jinja_root_names("{{ value | upper }}") == {"value"}


def test_jinja_root_names_no_interpolation() -> None:
    assert extract_jinja_root_names("no jinja here") == set()


# --- pick_field_name_for_action --------------------------------------------


def _input_action(text: str, intention: str = "", action_id: str = "a1") -> dict:
    return {
        "action_type": ActionType.INPUT_TEXT,
        "text": text,
        "intention": intention,
        "action_id": action_id,
    }


def test_rule_1_jinja_reference_to_declared_param() -> None:
    """If the goal contains `{{ key }}` and `key` is a declared param, use it."""
    pick = pick_field_name_for_action(
        action=_input_action("quantum computing", intention="search term"),
        goal_template="Search for preprints about {{ search_term }}.",
        declared_param_keys=frozenset({"search_term"}),
        upstream_schema_keys=frozenset(),
    )
    assert pick.field_name == "search_term"
    assert pick.rule == "jinja_ref"


def test_rule_1_falls_through_on_multi_key_goal() -> None:
    """When the goal references multiple valid keys, Rule 1 can't disambiguate
    which INPUT_TEXT action targets which key. Fall through to Rule 3 to avoid
    collapsing all actions onto the same name (CORR-1 from debate review)."""
    pick = pick_field_name_for_action(
        action=_input_action("x", intention="pick one"),
        goal_template="use {{ name_declared }} and {{ name_schema }}",
        declared_param_keys=frozenset({"name_declared"}),
        upstream_schema_keys=frozenset({"name_schema"}),
    )
    # Multi-key → Rule 3 fallback, not Rule 1
    assert pick.rule == "intention_derived"


def test_rule_1_ignores_jinja_refs_not_in_valid_keys() -> None:
    """A `{{ made_up }}` in the goal where `made_up` isn't declared → fall through."""
    pick = pick_field_name_for_action(
        action=_input_action("quantum computing", intention="search term for preprints"),
        goal_template="Search for {{ made_up_name }}",
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
    )
    # No match → Rule 3 fallback
    assert pick.rule == "intention_derived"


def test_rule_2_upstream_schema_via_intention_substring() -> None:
    """Intention text mentions an upstream schema key → use it."""
    pick = pick_field_name_for_action(
        action=_input_action("2026-04-15", intention="fill the invoice_date field"),
        goal_template="Download the invoice for the extracted date",
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset({"invoice_date"}),
    )
    assert pick.field_name == "invoice_date"
    assert pick.rule == "upstream_schema"


def test_rule_3_fallback_from_intention() -> None:
    """No declared param, no schema match → sanitize intention."""
    pick = pick_field_name_for_action(
        action=_input_action("quantum computing", intention="Enter the search term for preprints"),
        goal_template="Search for 'quantum computing' preprints.",
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
    )
    assert pick.rule == "intention_derived"
    assert pick.field_name.startswith("enter_the_search_term")


def test_existing_field_name_wins_unconditionally() -> None:
    """A preserved assignment from a cached block beats every rule."""
    pick = pick_field_name_for_action(
        action=_input_action("X", intention="whatever"),
        goal_template="{{ completely_different_param }}",
        declared_param_keys=frozenset({"completely_different_param"}),
        upstream_schema_keys=frozenset(),
        existing_field_name="legacy_name",
    )
    assert pick.field_name == "legacy_name"
    assert pick.rule == "existing_assignment"


# --- pick_field_names_for_actions (bulk) -----------------------------------


def test_bulk_picks_skip_empty_values() -> None:
    actions = {
        "t1": [
            {
                "action_type": ActionType.INPUT_TEXT,
                "text": "",
                "intention": "empty",
                "action_id": "a1",
            },
            {
                "action_type": ActionType.INPUT_TEXT,
                "text": "real",
                "intention": "real value",
                "action_id": "a2",
            },
        ]
    }
    picks = pick_field_names_for_actions(
        actions_by_task=actions,
        goal_template_by_task={"t1": ""},
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
    )
    assert "t1:a1" not in picks
    assert "t1:a2" in picks


def test_bulk_skips_non_custom_field_actions() -> None:
    actions = {
        "t1": [
            {
                "action_type": ActionType.CLICK,
                "action_id": "a1",
            },
            {
                "action_type": ActionType.INPUT_TEXT,
                "text": "foo",
                "intention": "type foo",
                "action_id": "a2",
            },
        ]
    }
    picks = pick_field_names_for_actions(
        actions_by_task=actions,
        goal_template_by_task={"t1": ""},
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
    )
    assert list(picks.keys()) == ["t1:a2"]


def test_bulk_applies_existing_assignments_by_counter_order() -> None:
    actions = {
        "t1": [
            {
                "action_type": ActionType.INPUT_TEXT,
                "text": "x",
                "intention": "first",
                "action_id": "a1",
            },
            {
                "action_type": ActionType.INPUT_TEXT,
                "text": "y",
                "intention": "second",
                "action_id": "a2",
            },
        ]
    }
    # 1-indexed counter — assignment for the second action
    picks = pick_field_names_for_actions(
        actions_by_task=actions,
        goal_template_by_task={"t1": ""},
        declared_param_keys=frozenset(),
        upstream_schema_keys=frozenset(),
        existing_field_assignments={2: "locked_name"},
    )
    assert picks["t1:a2"].field_name == "locked_name"
    assert picks["t1:a2"].rule == "existing_assignment"
    # First action unchanged
    assert picks["t1:a1"].rule == "intention_derived"
