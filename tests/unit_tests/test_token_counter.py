"""Tests for token counter utility."""

from skyvern.utils.token_counter import count_tokens


class TestCountTokens:
    """Tests for count_tokens function."""

    def test_empty_string(self):
        """Empty string should have 0 tokens."""
        assert count_tokens("") == 0

    def test_single_word(self):
        """Single word should return token count."""
        result = count_tokens("hello")
        assert result > 0
        assert isinstance(result, int)

    def test_simple_sentence(self):
        """Simple sentence should have reasonable token count."""
        result = count_tokens("Hello, world!")
        assert result > 0
        # "Hello, world!" typically tokenizes to ~4 tokens
        assert result < 10

    def test_longer_text(self):
        """Longer text should have more tokens."""
        short = count_tokens("Hi")
        long = count_tokens("This is a much longer sentence with many more words in it.")
        assert long > short

    def test_returns_integer(self):
        """Should return an integer."""
        result = count_tokens("test")
        assert isinstance(result, int)

    def test_whitespace_only(self):
        """Whitespace should be tokenized."""
        result = count_tokens("   ")
        # Whitespace is typically tokenized
        assert isinstance(result, int)

    def test_special_characters(self):
        """Special characters should be tokenized."""
        result = count_tokens("!@#$%^&*()")
        assert result > 0

    def test_numbers(self):
        """Numbers should be tokenized."""
        result = count_tokens("12345")
        assert result > 0

    def test_unicode(self):
        """Unicode characters should be tokenized."""
        result = count_tokens("你好世界")
        assert result > 0

    def test_mixed_content(self):
        """Mixed content (text, numbers, special chars) should work."""
        result = count_tokens("Hello123!@#World")
        assert result > 0

    def test_newlines(self):
        """Text with newlines should be tokenized."""
        result = count_tokens("Hello\nWorld\nTest")
        assert result > 0

    def test_code_snippet(self):
        """Code snippets should be tokenized."""
        code = """
def hello():
    print("Hello, World!")
"""
        result = count_tokens(code)
        assert result > 5  # Code should have multiple tokens

    def test_json_content(self):
        """JSON content should be tokenized."""
        json_str = '{"key": "value", "number": 123}'
        result = count_tokens(json_str)
        assert result > 0

    def test_url(self):
        """URLs should be tokenized."""
        result = count_tokens("https://www.example.com/path?query=value")
        assert result > 0

    def test_consistency(self):
        """Same input should always produce same output."""
        text = "This is a test sentence."
        result1 = count_tokens(text)
        result2 = count_tokens(text)
        assert result1 == result2

    def test_very_long_text(self):
        """Very long text should be handled."""
        long_text = "word " * 1000
        result = count_tokens(long_text)
        assert result > 100  # Should have many tokens

    def test_token_count_approximation(self):
        """Token count should be roughly 1 token per 4 chars for English."""
        text = "This is a sample text for testing token count approximation."
        result = count_tokens(text)
        # GPT tokenizers typically produce ~1 token per 4 characters
        char_count = len(text)
        assert result > char_count / 10  # Very loose lower bound
        assert result < char_count  # Token count should be less than char count
