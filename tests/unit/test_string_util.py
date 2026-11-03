from skyvern.webeye.string_util import remove_whitespace


class TestRemoveWhitespace:
    """Tests for the remove_whitespace function."""

    def test_remove_mixed_whitespace(self):
        """Mixed whitespace (spaces, tabs, newlines) should be collapsed."""
        assert remove_whitespace("hello \t\n  world") == "hello world"
