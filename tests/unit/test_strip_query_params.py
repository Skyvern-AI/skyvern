from __future__ import annotations

import pytest  # type: ignore[import-not-found]

from skyvern.utils.url_validators import strip_query_params


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/path?token=secret&id=1", "https://example.com/path"),
        ("https://example.com/path#fragment", "https://example.com/path"),
        ("https://example.com/path?q=1#frag", "https://example.com/path"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com", "https://example.com"),
        ("http://localhost:8000/api/v1/tasks", "http://localhost:8000/api/v1/tasks"),
        # Credentials in URL — must be stripped to prevent PII leakage
        ("https://user:password@example.com/path?token=x", "https://example.com/path"),
        ("https://admin:secret@host.com:8443/api", "https://host.com:8443/api"),
        # Edge cases that should return empty string
        ("", ""),
        ("example.com/path", ""),
        ("not-a-url", ""),
        ("/relative/path", ""),
        ("://missing-scheme", ""),
    ],
)
def test_strip_query_params(url: str, expected: str) -> None:
    assert strip_query_params(url) == expected
