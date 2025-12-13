"""Extended tests for URL validators module."""

import pytest

from skyvern.exceptions import InvalidUrl
from skyvern.utils.url_validators import encode_url, prepend_scheme_and_validate_url


class TestEncodeUrl:
    """Tests for encode_url function."""

    def test_encode_simple_path(self):
        """Simple path with spaces should be encoded."""
        url = "https://example.com/path with spaces"
        result = encode_url(url)
        assert result == "https://example.com/path%20with%20spaces"

    def test_encode_query_params(self):
        """Query parameters with spaces should be encoded."""
        url = "https://example.com/search?q=hello world"
        result = encode_url(url)
        assert result == "https://example.com/search?q=hello%20world"

    def test_preserve_slashes_in_path(self):
        """Slashes in path should be preserved."""
        url = "https://example.com/path/to/resource"
        result = encode_url(url)
        assert result == "https://example.com/path/to/resource"

    def test_preserve_existing_encoding(self):
        """Already encoded characters should be preserved."""
        url = "https://example.com/path%20already%20encoded"
        result = encode_url(url)
        assert result == "https://example.com/path%20already%20encoded"

    def test_encode_special_characters(self):
        """Special characters in path should be encoded."""
        url = "https://example.com/path<with>brackets"
        result = encode_url(url)
        # Angle brackets should be percent-encoded
        assert result == "https://example.com/path%3Cwith%3Ebrackets"

    def test_preserve_query_structure(self):
        """Query string structure (= and &) should be preserved."""
        url = "https://example.com/search?key1=value1&key2=value2"
        result = encode_url(url)
        assert "key1=value1" in result
        assert "key2=value2" in result
        assert "&" in result

    def test_empty_path(self):
        """URL with empty path should work."""
        url = "https://example.com"
        result = encode_url(url)
        assert result == "https://example.com"

    def test_encode_unicode_path(self):
        """Unicode characters in path should be encoded."""
        url = "https://example.com/路径"
        result = encode_url(url)
        # Unicode should be percent-encoded, original characters should not appear
        assert "路径" not in result
        assert "example.com/" in result
        # "路径" in UTF-8 is encoded as %E8%B7%AF%E5%BE%84
        assert "%E8%B7%AF%E5%BE%84" in result

    def test_fragment_preserved(self):
        """URL fragments should be preserved."""
        url = "https://example.com/page#section"
        result = encode_url(url)
        # Fragment should be preserved in the output
        assert result == "https://example.com/page#section"


class TestPrependSchemeAndValidateUrl:
    """Tests for prepend_scheme_and_validate_url function."""

    def test_empty_url_returns_empty(self):
        """Empty URL should return empty string."""
        assert prepend_scheme_and_validate_url("") == ""

    def test_https_url_unchanged(self):
        """URL with https scheme should remain unchanged."""
        url = "https://example.com"
        result = prepend_scheme_and_validate_url(url)
        assert result == url

    def test_http_url_unchanged(self):
        """URL with http scheme should remain unchanged."""
        url = "http://example.com"
        result = prepend_scheme_and_validate_url(url)
        assert result == url

    def test_no_scheme_gets_https(self):
        """URL without scheme should get https prepended."""
        url = "example.com"
        result = prepend_scheme_and_validate_url(url)
        assert result == "https://example.com"

    def test_invalid_scheme_raises_error(self):
        """URL with invalid scheme should raise InvalidUrl."""
        with pytest.raises(InvalidUrl):
            prepend_scheme_and_validate_url("ftp://example.com")

    def test_file_scheme_raises_error(self):
        """URL with file scheme should raise InvalidUrl."""
        with pytest.raises(InvalidUrl):
            prepend_scheme_and_validate_url("file:///etc/passwd")

    def test_javascript_scheme_raises_error(self):
        """URL with javascript scheme should raise InvalidUrl."""
        with pytest.raises(InvalidUrl):
            prepend_scheme_and_validate_url("javascript:alert(1)")

    def test_valid_url_with_path(self):
        """Valid URL with path should work."""
        url = "https://example.com/path/to/resource"
        result = prepend_scheme_and_validate_url(url)
        assert result == url

    def test_valid_url_with_query(self):
        """Valid URL with query parameters should work."""
        url = "https://example.com/search?q=test"
        result = prepend_scheme_and_validate_url(url)
        assert result == url

    def test_url_with_port(self):
        """URL with port number should work."""
        url = "https://example.com:8080/path"
        result = prepend_scheme_and_validate_url(url)
        assert result == url

    def test_subdomain_url(self):
        """URL with subdomain should work."""
        url = "https://api.example.com/v1"
        result = prepend_scheme_and_validate_url(url)
        assert result == url

    def test_invalid_url_raises_error(self):
        """Completely invalid URL should raise InvalidUrl."""
        with pytest.raises(InvalidUrl):
            prepend_scheme_and_validate_url("not a valid url at all!!!")


class TestEncodeUrlEdgeCases:
    """Edge case tests for encode_url."""

    def test_double_slashes_in_path(self):
        """Double slashes in path should be preserved."""
        url = "https://example.com//double//slashes"
        result = encode_url(url)
        assert "//double//slashes" in result

    def test_url_with_credentials(self):
        """URL with credentials should preserve the authority portion."""
        url = "https://user:pass@example.com/path"
        result = encode_url(url)
        # Full authority portion including credentials should be preserved
        assert "user:pass@example.com" in result

    def test_very_long_url(self):
        """Very long URL should be handled."""
        long_path = "/a" * 1000
        url = f"https://example.com{long_path}"
        result = encode_url(url)
        assert len(result) >= len(url)

    def test_url_with_multiple_query_params(self):
        """URL with multiple query parameters should work."""
        url = "https://example.com/search?a=1&b=2&c=3&d=4&e=5"
        result = encode_url(url)
        assert "a=1" in result
        assert "e=5" in result
