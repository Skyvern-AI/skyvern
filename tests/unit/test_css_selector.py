"""Tests for compute_stable_selector and _looks_dynamic."""

from skyvern.utils.css_selector import _looks_dynamic, compute_stable_selector


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
    """Tests for compute_stable_selector priority and output."""

    def test_prefers_text_over_dynamic_id(self):
        """When ID is dynamic and text is available, use :has-text()."""
        elem = {
            "tagName": "button",
            "text": "Apply Filters",
            "attributes": {"id": "ember12345678", "type": "submit"},
        }
        result = compute_stable_selector(elem)
        assert result == 'button:has-text("Apply Filters")'

    def test_static_id_preferred_over_text(self):
        """When ID looks stable, use it (it's higher priority)."""
        elem = {
            "tagName": "button",
            "text": "Submit",
            "attributes": {"id": "signOnButton"},
        }
        result = compute_stable_selector(elem)
        assert result == "#signOnButton"

    def test_aria_label_preferred_over_text(self):
        elem = {
            "tagName": "input",
            "text": "",
            "attributes": {"aria-label": "Email Address", "id": "uid-12345"},
        }
        result = compute_stable_selector(elem)
        assert result == 'input[aria-label="Email Address"]'

    def test_realistic_scraper_shape(self):
        """Scraper output shape (from domUtils.js buildElementObject) should work directly.

        el_data has top-level 'id' (Skyvern uniqueId), 'tagName', 'text',
        and nested 'attributes' with real HTML attrs. The scraper stores
        unique_id as a separate attribute, not as 'id'.
        """
        el_data = {
            "id": "AAGD",  # Skyvern unique_id — NOT the HTML id
            "tagName": "button",
            "attributes": {"unique_id": "AAGD", "type": "submit"},
            "text": "Apply Filters",
            "xpath": "/html/body/div/button",
        }
        result = compute_stable_selector(el_data)
        assert result == 'button:has-text("Apply Filters")'

    def test_realistic_scraper_shape_with_aria_label(self):
        """Scraper shape with aria-label should produce aria-label selector."""
        el_data = {
            "id": "AABC",
            "tagName": "input",
            "attributes": {"unique_id": "AABC", "aria-label": "Email Address", "type": "email"},
            "text": "",
        }
        result = compute_stable_selector(el_data)
        assert result == 'input[aria-label="Email Address"]'

    def test_realistic_scraper_shape_with_stable_html_id(self):
        """When the HTML id is stable (same as Skyvern id only by coincidence), use it."""
        el_data = {
            "id": "AAGD",
            "tagName": "button",
            "attributes": {"id": "signOnButton", "type": "submit"},
            "text": "Sign On",
        }
        result = compute_stable_selector(el_data)
        assert result == "#signOnButton"

    def test_no_data_returns_none(self):
        assert compute_stable_selector(None) is None
        assert compute_stable_selector({}) is None
