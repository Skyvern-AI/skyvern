"""Tests for compute_stable_selector, compute_selector_options, and _looks_dynamic."""

import pytest

from skyvern.utils.css_selector import _looks_dynamic, compute_selector_options, compute_stable_selector


class TestLooksDynamic:
    """Tests for the _looks_dynamic heuristic."""

    def test_long_hex_ids_are_dynamic(self):
        assert _looks_dynamic("ember12345678") is True
        assert _looks_dynamic("react-abcdef01") is True

    def test_word_digit_ids_are_dynamic(self):
        assert _looks_dynamic("uid-12345") is True
        assert _looks_dynamic("el_56789") is True

    def test_semantic_ids_are_not_dynamic(self):
        """Meaningful IDs should NOT be flagged as dynamic."""
        assert _looks_dynamic("username") is False
        assert _looks_dynamic("sign_in") is False
        assert _looks_dynamic("passwd") is False
        assert _looks_dynamic("inputUsername") is False
        assert _looks_dynamic("login-form") is False
        assert _looks_dynamic("signOnButton") is False

    def test_single_char_ids_not_dynamic(self):
        """Very short IDs shouldn't match the all-caps pattern."""
        assert _looks_dynamic("A") is False
        assert _looks_dynamic("AB") is False

    def test_title_case_ids_not_dynamic(self):
        """Title-case or mixed-case IDs are likely meaningful, not generated."""
        assert _looks_dynamic("Login") is False
        assert _looks_dynamic("Form") is False
        assert _looks_dynamic("Nav") is False
        assert _looks_dynamic("Input") is False
        assert _looks_dynamic("Button") is False


class TestComputeStableSelector:
    """Tests for compute_stable_selector — returns top-ranked option."""

    def test_prefers_name_over_dynamic_id(self):
        """When ID is dynamic and name is available, use name."""
        elem = {
            "tagName": "input",
            "text": "",
            "attributes": {"id": "input61", "name": "credentials.passcode", "type": "password"},
        }
        result = compute_stable_selector(elem)
        assert result == 'input[name="credentials.passcode"]'

    def test_static_id_still_returned_when_no_semantic(self):
        """When only a stable ID is available, it's returned."""
        elem = {
            "tagName": "div",
            "text": "",
            "attributes": {"id": "signOnButton"},
        }
        result = compute_stable_selector(elem)
        assert result == "#signOnButton"

    def test_aria_label_ranked_above_id(self):
        """aria-label is preferred over even stable IDs."""
        elem = {
            "tagName": "input",
            "text": "",
            "attributes": {"aria-label": "Email Address", "id": "email-field"},
        }
        result = compute_stable_selector(elem)
        assert result == 'input[aria-label="Email Address"]'

    def test_no_data_returns_none(self):
        assert compute_stable_selector(None) is None
        assert compute_stable_selector({}) is None


class TestComputeSelectorOptions:
    """Tests for compute_selector_options — returns ALL viable selectors ranked."""

    def test_returns_all_options_ranked(self):
        """Element with multiple attributes produces multiple options."""
        elem = {
            "tagName": "input",
            "text": "",
            "attributes": {
                "name": "credentials.passcode",
                "id": "input61",
                "placeholder": "Enter passcode",
                "type": "password",
            },
        }
        options = compute_selector_options(elem)
        selectors = [s for s, _ in options]

        # name should be ranked above the ID (input61 is not caught by _looks_dynamic
        # but name is ranked higher than IDs regardless)
        assert 'input[name="credentials.passcode"]' in selectors
        assert "#input61" in selectors
        assert 'input[placeholder="Enter passcode"]' in selectors
        assert 'input[type="password"]' in selectors

        # name should come before ID in the ranking
        name_idx = selectors.index('input[name="credentials.passcode"]')
        id_idx = selectors.index("#input61")
        assert name_idx < id_idx

    def test_dynamic_id_marked_as_possibly_generated(self):
        """IDs that look auto-generated are included but flagged."""
        elem = {
            "tagName": "button",
            "text": "Submit",
            "attributes": {"id": "ember12345678", "type": "submit"},
        }
        options = compute_selector_options(elem)
        id_options = [(s, d) for s, d in options if s == "#ember12345678"]
        assert len(id_options) == 1
        assert "auto-generated" in id_options[0][1]

    def test_stable_id_not_marked_dynamic(self):
        """Stable IDs get a clean description."""
        elem = {
            "tagName": "button",
            "text": "Sign On",
            "attributes": {"id": "signOnButton"},
        }
        options = compute_selector_options(elem)
        id_options = [(s, d) for s, d in options if s == "#signOnButton"]
        assert len(id_options) == 1
        assert "auto-generated" not in id_options[0][1]
        assert id_options[0][1] == "HTML id"

    def test_data_testid_ranked_first(self):
        """data-testid is the most stable selector."""
        elem = {
            "tagName": "input",
            "text": "",
            "attributes": {"data-testid": "email-input", "name": "email", "id": "field-1"},
        }
        options = compute_selector_options(elem)
        assert options[0] == ('[data-testid="email-input"]', "stable test attribute")

    def test_empty_element_returns_empty(self):
        assert compute_selector_options(None) == []
        assert compute_selector_options({}) == []

    def test_single_option_element(self):
        """Element with only one viable attribute produces one option."""
        elem = {
            "tagName": "div",
            "text": "",
            "attributes": {"role": "dialog"},
        }
        options = compute_selector_options(elem)
        assert len(options) == 1
        assert options[0] == ('div[role="dialog"]', "ARIA role")

    def test_button_text_included(self):
        """Buttons with short text get a :has-text() option."""
        elem = {
            "tagName": "button",
            "text": "Apply Filters",
            "attributes": {},
        }
        options = compute_selector_options(elem)
        assert ('button:has-text("Apply Filters")', "visible text") in options

    @pytest.mark.parametrize(
        "elem_id, expected",
        [
            # Bare "#<id>" only parses for CSS identifiers.
            ("signOnButton", "#signOnButton"),
            ("field-1", "#field-1"),
            ("_private", "#_private"),
            ("-dashed", "#-dashed"),
            # A CSS identifier cannot start with a digit: "#123abc" is an invalid selector.
            ("123abc", '[id="123abc"]'),
            ("-1abc", '[id="-1abc"]'),
            # Whitespace would make "#a b" parse as "#a" descendant "b" — a different element.
            ("a b", '[id="a b"]'),
            # Punctuation needs escaping under "#", so use the attribute form instead.
            ("foo.bar", '[id="foo.bar"]'),
            ("a:b", '[id="a:b"]'),
            ('quo"te', '[id="quo\\"te"]'),
            ("back\\slash", '[id="back\\\\slash"]'),
        ],
    )
    def test_id_selector_is_a_parseable_selector_for_that_id(self, elem_id, expected):
        """Ids that aren't CSS identifiers must use [id="..."], not "#<id>"."""
        options = compute_selector_options({"tagName": "div", "text": "", "attributes": {"id": elem_id}})
        assert [s for s, _ in options] == [expected]

    def test_dynamic_id_also_uses_attribute_form_when_not_an_identifier(self):
        """The auto-generated-id branch builds its selector the same way as the stable one."""
        elem = {"tagName": "div", "text": "", "attributes": {"id": "1234abcdef"}}
        options = compute_selector_options(elem)
        assert len(options) == 1
        selector, description = options[0]
        assert selector == '[id="1234abcdef"]'
        assert "auto-generated" in description

    def test_input61_case(self):
        """The exact case that motivated this change — input61 with a name attribute."""
        elem = {
            "tagName": "input",
            "text": "",
            "attributes": {
                "id": "input61",
                "name": "credentials.passcode",
                "type": "password",
            },
        }
        options = compute_selector_options(elem)
        # Should have at least name, id, and type options
        assert len(options) >= 3
        # Name should be #1 (ranked above ID)
        assert options[0][0] == 'input[name="credentials.passcode"]'
        assert options[0][1] == "form element name"
