"""Tests for string utility functions."""

import string

from skyvern.utils.strings import RANDOM_STRING_POOL, generate_random_string


class TestRandomStringPool:
    """Tests for RANDOM_STRING_POOL constant."""

    def test_pool_contains_letters(self):
        """Pool should contain all ASCII letters."""
        for char in string.ascii_letters:
            assert char in RANDOM_STRING_POOL

    def test_pool_contains_digits(self):
        """Pool should contain all digits."""
        for char in string.digits:
            assert char in RANDOM_STRING_POOL

    def test_pool_size(self):
        """Pool should have expected size (26*2 + 10 = 62)."""
        assert len(RANDOM_STRING_POOL) == 62


class TestGenerateRandomString:
    """Tests for generate_random_string function."""

    def test_default_length(self):
        """Default length should be 5."""
        result = generate_random_string()
        assert len(result) == 5

    def test_custom_length(self):
        """Custom length should be respected."""
        for length in [1, 10, 50, 100]:
            result = generate_random_string(length)
            assert len(result) == length

    def test_zero_length(self):
        """Zero length should return empty string."""
        result = generate_random_string(0)
        assert result == ""

    def test_returns_string(self):
        """Should return a string type."""
        result = generate_random_string()
        assert isinstance(result, str)

    def test_only_alphanumeric(self):
        """Result should only contain alphanumeric characters."""
        result = generate_random_string(100)
        for char in result:
            assert char in RANDOM_STRING_POOL

    def test_randomness(self):
        """Multiple calls should produce different results (with high probability)."""
        results = [generate_random_string(20) for _ in range(10)]
        # All results should be unique (statistically extremely likely with length 20)
        assert len(set(results)) == 10

    def test_distribution(self):
        """Characters should be reasonably distributed."""
        # Generate a long string and check distribution
        result = generate_random_string(10000)
        char_counts = {}
        for char in result:
            char_counts[char] = char_counts.get(char, 0) + 1

        # Each character should appear at least once in 10000 characters
        # (statistically extremely likely)
        assert len(char_counts) > 50  # Most of the 62 chars should appear

    def test_contains_letters(self):
        """Generated strings should typically contain letters."""
        # With 62 possible chars and length 100, very likely to have letters
        result = generate_random_string(100)
        has_letter = any(c in string.ascii_letters for c in result)
        assert has_letter

    def test_contains_digits(self):
        """Generated strings should typically contain digits."""
        # With 62 possible chars and length 100, very likely to have digits
        result = generate_random_string(100)
        has_digit = any(c in string.digits for c in result)
        assert has_digit

    def test_large_length(self):
        """Should handle large lengths."""
        result = generate_random_string(10000)
        assert len(result) == 10000
