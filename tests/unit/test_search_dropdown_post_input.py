"""Regression tests for post-input search-combobox dropdown handling."""

from __future__ import annotations

from skyvern.webeye.actions.handler import _incremental_tree_contains_target_value

# ---------------------------------------------------------------------------
# _incremental_tree_contains_target_value unit tests
# ---------------------------------------------------------------------------


def test_incremental_tree_contains_formatted_search_dropdown_value() -> None:
    incremental_elements = [
        {
            "id": "AACz",
            "tagName": "div",
            "children": [
                {
                    "id": "AADo",
                    "tagName": "div",
                    "children": [{"id": "AADp", "tagName": "span", "text": "(CODE) 12345678"}],
                }
            ],
        }
    ]

    assert _incremental_tree_contains_target_value(incremental_elements, "12345678")


def test_incremental_tree_matches_hyphenated_label() -> None:
    elements = [{"id": "opt", "tagName": "span", "text": "BA-12345678 - Main Account"}]
    assert _incremental_tree_contains_target_value(elements, "12345678")


def test_incremental_tree_ignores_unrelated_search_suggestions() -> None:
    incremental_elements = [
        {
            "id": "suggestions",
            "tagName": "div",
            "children": [{"id": "option", "tagName": "span", "text": "Account overview"}],
        }
    ]

    assert not _incremental_tree_contains_target_value(incremental_elements, "12345678")


def test_incremental_tree_matches_attribute_value() -> None:
    elements = [{"id": "opt", "tagName": "div", "attributes": {"data-value": "12345678"}}]
    assert _incremental_tree_contains_target_value(elements, "12345678")


def test_incremental_tree_empty_target_returns_false() -> None:
    elements = [{"id": "opt", "tagName": "span", "text": "anything"}]
    assert not _incremental_tree_contains_target_value(elements, "")


def test_incremental_tree_case_insensitive_match() -> None:
    elements = [{"id": "opt", "tagName": "span", "text": "John Smith"}]
    assert _incremental_tree_contains_target_value(elements, "john")


def test_incremental_tree_no_match_on_empty_elements() -> None:
    assert not _incremental_tree_contains_target_value([], "12345678")
