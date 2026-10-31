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
        # A netloc the parser cannot interpret must degrade to "" instead of raising: the callers
        # pass unvalidated strings (``Task.totp_verification_url``, page URLs from the browser).
        ("https://example.com:99999/path?token=x", ""),
        ("https://example.com:notaport/path", ""),
        ("http://[::1", ""),
    ],
)
def test_strip_query_params(url: str, expected: str) -> None:
    assert strip_query_params(url) == expected


def test_strip_query_params_returns_empty_instead_of_raising_for_unparsable_netloc() -> None:
    """A malformed netloc must not raise out of this display/identity helper.

    ``Task.totp_verification_url`` is an unvalidated string, so a typo'd port reaches the
    span/log helpers verbatim; ``auth_tools._loggable_url`` already wraps this call for
    exactly that reason. Returning "" keeps the bad URL out of the log without costing the
    caller its exception handling.
    """
    for url in ("https://example.com:99999/hook?token=abc", "https://example.com:notaport/x", "http://[::1"):
        assert strip_query_params(url) == ""
