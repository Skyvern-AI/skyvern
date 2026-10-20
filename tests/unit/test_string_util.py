from skyvern.webeye.string_util import remove_whitespace


class TestRemoveWhitespace:
    """Tests for the remove_whitespace function."""

    def test_remove_multiple_spaces(self):
        """Multiple spaces should be collapsed to single space."""
        assert remove_whitespace("hello    world") == "hello world"

    def test_remove_tabs(self):
        """Tab characters should be converted to single space."""
        assert remove_whitespace("hello\tworld") == "hello world"

    def test_remove_newlines(self):
        """Newline characters should be converted to single space."""
        assert remove_whitespace("hello\nworld") == "hello world"

    def test_remove_mixed_whitespace(self):
        """Mixed whitespace (spaces, tabs, newlines) should be collapsed."""
        assert remove_whitespace("hello \t\n  world") == "hello world"

    def test_leading_trailing_whitespace(self):
        """Leading and trailing whitespace should be collapsed but not removed."""
        assert remove_whitespace("  hello  ") == " hello "

    def test_empty_string(self):
        """Empty string should return empty string."""
        assert remove_whitespace("") == ""

    def test_single_space(self):
        """Single space should remain unchanged."""
        assert remove_whitespace(" ") == " "

    def test_no_whitespace(self):
        """String without extra whitespace should remain unchanged."""
        assert remove_whitespace("hello") == "hello"

    def test_only_whitespace(self):
        """String of only whitespace should collapse to single space."""
        assert remove_whitespace("   \t\n   ") == " "

    def test_multiline_text(self):
        """Multiline text should have all whitespace collapsed."""
        input_text = """Hello
        World
        Test"""
        assert remove_whitespace(input_text) == "Hello World Test"

    def test_preserves_non_whitespace_special_chars(self):
        """Non-whitespace special characters should be preserved."""
        assert remove_whitespace("hello!@#$%^&*()world") == "hello!@#$%^&*()world"

    def test_unicode_text(self):
        """Unicode text with whitespace should work correctly."""
        assert remove_whitespace("你好  世界") == "你好 世界"

    def test_carriage_return_not_matched(self):
        """Carriage return is not in the regex pattern, verify behavior."""
        # Note: \r is not in the original regex pattern [ \n\t]+
        # This test documents the current behavior
        result = remove_whitespace("hello\rworld")
        assert result == "hello\rworld"
