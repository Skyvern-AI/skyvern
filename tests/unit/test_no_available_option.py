"""Unit tests for the custom-select no-match path."""

from __future__ import annotations

import pytest

from skyvern.exceptions import (
    NoAvailableOptionFoundForCustomSelection,
    NoIncrementalElementFoundForCustomSelection,
)
from skyvern.webeye.actions.handler import (
    _collect_option_texts,
    _no_match_exception_for_dropdown,
)


class TestCollectOptionTexts:
    def test_extracts_li_option_texts(self) -> None:
        tree = [
            {
                "tagName": "ul",
                "attributes": {"role": "listbox"},
                "children": [
                    {"tagName": "li", "attributes": {"role": "option"}, "text": "Alpha"},
                    {"tagName": "li", "attributes": {"role": "option"}, "text": "Bravo"},
                    {"tagName": "li", "attributes": {"role": "option"}, "text": "Charlie"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Alpha", "Bravo", "Charlie"]

    def test_extracts_native_option_elements(self) -> None:
        tree = [
            {
                "tagName": "select",
                "children": [
                    {"tagName": "option", "text": "First"},
                    {"tagName": "option", "text": "Second"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["First", "Second"]

    def test_ignores_non_option_nodes(self) -> None:
        tree = [
            {"tagName": "div", "text": "header copy"},
            {"tagName": "button", "text": "Submit"},
            {"tagName": "span", "text": "label"},
        ]
        assert _collect_option_texts(tree) == []

    def test_returns_empty_for_empty_tree(self) -> None:
        assert _collect_option_texts([]) == []

    def test_handles_missing_optional_fields(self) -> None:
        tree = [
            {"tagName": "li"},
            {"tagName": "li", "attributes": {"role": "option"}, "text": ""},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "  "},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Real"},
        ]
        assert _collect_option_texts(tree) == ["Real"]

    def test_dedupes_repeated_option_text(self) -> None:
        tree = [
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Alpha"},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Bravo"},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Alpha"},
        ]
        assert _collect_option_texts(tree) == ["Alpha", "Bravo"]

    def test_walks_nested_children(self) -> None:
        tree = [
            {
                "tagName": "div",
                "children": [
                    {
                        "tagName": "ul",
                        "attributes": {"role": "listbox"},
                        "children": [
                            {"tagName": "li", "attributes": {"role": "option"}, "text": "Inner"},
                        ],
                    }
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Inner"]

    def test_extracts_div_role_option(self) -> None:
        tree = [
            {
                "tagName": "div",
                "attributes": {"role": "listbox"},
                "children": [
                    {"tagName": "div", "attributes": {"role": "option"}, "text": "Alpha"},
                    {"tagName": "div", "attributes": {"role": "option"}, "text": "Bravo"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Alpha", "Bravo"]

    def test_extracts_native_select_from_options_field(self) -> None:
        # Scraper stores native <select> options on the element itself and
        # skips child <option> nodes.
        tree = [
            {
                "tagName": "select",
                "options": [
                    {"optionIndex": 0, "text": "January", "value": "1"},
                    {"optionIndex": 1, "text": "February", "value": "2"},
                    {"optionIndex": 2, "text": "March", "value": "3"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["January", "February", "March"]

    def test_falls_back_to_value_when_options_text_is_empty(self) -> None:
        tree = [
            {
                "tagName": "select",
                "options": [
                    {"optionIndex": 0, "text": "", "value": "Q1"},
                    {"optionIndex": 1, "text": "Two", "value": "2"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Q1", "Two"]

    def test_falls_back_to_value_when_options_text_is_whitespace_only(self) -> None:
        tree = [
            {
                "tagName": "select",
                "options": [
                    {"optionIndex": 0, "text": "   ", "value": "Q1"},
                    {"optionIndex": 1, "text": "\t\n", "value": "Q2"},
                    {"optionIndex": 2, "text": "Real", "value": "x"},
                ],
            }
        ]
        assert _collect_option_texts(tree) == ["Q1", "Q2", "Real"]

    def test_dedupes_across_li_and_options_field(self) -> None:
        tree = [
            {
                "tagName": "select",
                "options": [{"optionIndex": 0, "text": "Alpha", "value": "a"}],
            },
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Alpha"},
            {"tagName": "li", "attributes": {"role": "option"}, "text": "Bravo"},
        ]
        assert _collect_option_texts(tree) == ["Alpha", "Bravo"]


class TestNoAvailableOptionFoundForCustomSelection:
    def test_message_includes_code_target_count_excerpt_and_reason(self) -> None:
        exc = NoAvailableOptionFoundForCustomSelection(
            reason="not present in the list",
            target_value="Target Value",
            observed_options=["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"],
        )
        msg = str(exc)
        assert "code=OPTION_NOT_AVAILABLE" in msg
        assert "target_value='Target Value'" in msg
        assert "observed_options_count=6" in msg
        assert "['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo']" in msg
        assert "Foxtrot" not in msg  # excerpt is capped at 5
        assert "not present in the list" in msg

    def test_attributes_are_accessible_for_downstream_consumers(self) -> None:
        exc = NoAvailableOptionFoundForCustomSelection(
            reason="not in dropdown",
            target_value="Target",
            observed_options=["Alpha", "Bravo"],
        )
        assert exc.code == "OPTION_NOT_AVAILABLE"
        assert exc.target_value == "Target"
        assert exc.observed_options_count == 2
        assert exc.observed_options_excerpt == ["Alpha", "Bravo"]
        assert exc.reason == "not in dropdown"

    def test_omits_optional_fields_when_not_supplied(self) -> None:
        exc = NoAvailableOptionFoundForCustomSelection(reason=None)
        msg = str(exc)
        assert "code=OPTION_NOT_AVAILABLE" in msg
        assert "target_value" not in msg
        assert "observed_options_count" not in msg
        assert "observed_options_excerpt" not in msg
        assert exc.target_value is None
        assert exc.observed_options_count == 0
        assert exc.observed_options_excerpt == []

    def test_no_value_error_when_constructed_from_empty_no_match_payload(self) -> None:
        # Regression: previously ActionType("") fired on this payload before the
        # OPTION_NOT_AVAILABLE branch could run.
        json_response = {"action_type": "", "id": "", "reasoning": "not present", "relevant": False}
        try:
            raise NoAvailableOptionFoundForCustomSelection(
                reason=json_response["reasoning"],
                target_value="Anything",
                observed_options=["Alpha"],
            )
        except ValueError:
            pytest.fail("ValueError leaked from no-match exception construction")
        except NoAvailableOptionFoundForCustomSelection as exc:
            assert exc.code == "OPTION_NOT_AVAILABLE"


class TestNoMatchExceptionForDropdown:
    def test_returns_transient_when_no_options_and_fallback_id_given(self) -> None:
        exc = _no_match_exception_for_dropdown(
            reasoning="dropdown empty",
            target_value="Target",
            observed_options=[],
            transient_fallback_element_id="element-123",
        )
        assert isinstance(exc, NoIncrementalElementFoundForCustomSelection)
        assert "element-123" in str(exc)

    def test_returns_permanent_when_options_observed(self) -> None:
        exc = _no_match_exception_for_dropdown(
            reasoning="target not in list",
            target_value="Target",
            observed_options=["Alpha", "Bravo"],
            transient_fallback_element_id="element-123",
        )
        assert isinstance(exc, NoAvailableOptionFoundForCustomSelection)
        assert exc.target_value == "Target"
        assert exc.observed_options_count == 2
        assert exc.observed_options_excerpt == ["Alpha", "Bravo"]
        assert exc.reason == "target not in list"

    def test_returns_permanent_when_no_options_but_no_fallback_id(self) -> None:
        # The emerging-element path passes None: an upstream guard handles the
        # zero-options case there, so this branch must surface as permanent.
        exc = _no_match_exception_for_dropdown(
            reasoning="target not in list",
            target_value="Target",
            observed_options=[],
            transient_fallback_element_id=None,
        )
        assert isinstance(exc, NoAvailableOptionFoundForCustomSelection)
        assert exc.observed_options_count == 0
        assert exc.observed_options_excerpt == []

    def test_normalizes_empty_target_value_to_none(self) -> None:
        exc = _no_match_exception_for_dropdown(
            reasoning=None,
            target_value="",
            observed_options=["Alpha"],
            transient_fallback_element_id=None,
        )
        assert isinstance(exc, NoAvailableOptionFoundForCustomSelection)
        assert exc.target_value is None

    def test_native_select_populated_routes_to_permanent_not_transient(self) -> None:
        # Regression: a native <select> populated via element["options"]
        # must NOT be misread as zero-options and routed to the transient
        # exception. Walker first, then helper, end-to-end on the F-guard.
        tree = [
            {
                "tagName": "select",
                "options": [
                    {"optionIndex": 0, "text": "January", "value": "1"},
                    {"optionIndex": 1, "text": "February", "value": "2"},
                ],
            }
        ]
        observed = _collect_option_texts(tree)
        assert observed == ["January", "February"]
        exc = _no_match_exception_for_dropdown(
            reasoning="target not in list",
            target_value="December",
            observed_options=observed,
            transient_fallback_element_id="select-element-id",
        )
        assert isinstance(exc, NoAvailableOptionFoundForCustomSelection)
        assert exc.observed_options_count == 2
