import pytest

from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.utils.url_validators import encode_url, is_blocked_host, validate_url


def test_encode_url_basic():
    """Test basic URL encoding with simple path"""
    url = "https://example.com/path with spaces"
    expected = "https://example.com/path%20with%20spaces"
    assert encode_url(url) == expected


def test_encode_url_with_query_params():
    """Test URL encoding with query parameters"""
    url = "https://example.com/search?q=hello world&type=test"
    expected = "https://example.com/search?q=hello%20world&type=test"
    assert encode_url(url) == expected


def test_encode_url_with_special_chars():
    """Test URL encoding with special characters"""
    url = "https://example.com/path/with/special#chars?param=value&other=test@123"
    expected = "https://example.com/path/with/special#chars?param=value&other=test@123"
    assert encode_url(url) == expected


def test_encode_url_with_pre_encoded_chars():
    """Test URL encoding with pre-encoded characters in query parameters"""
    url = "https://example.com/search?q=hello world&type=test%20test"
    expected = "https://example.com/search?q=hello%20world&type=test%20test"
    assert encode_url(url) == expected


@pytest.mark.parametrize(
    "host",
    [
        "[::1]",
        "[::ffff:127.0.0.1]",
        "[::ffff:7f00:1]",
        "[::ffff:169.254.169.254]",
        "[::ffff:a9fe:a9fe]",
        "[::ffff:10.0.0.1]",
        "[::ffff:192.168.1.1]",
        "[fe80::1]",
        "[fc00::1]",
    ],
)
def test_is_blocked_host_bracketed_ipv6_internal(host: str) -> None:
    assert is_blocked_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "fe80::1",
        "fc00::1",
        "10.0.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "localhost",
    ],
)
def test_is_blocked_host_unbracketed_internal(host: str) -> None:
    assert is_blocked_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "[2001:4860:4860::8888]",
        "2001:4860:4860::8888",
        "8.8.8.8",
        "example.com",
    ],
)
def test_is_blocked_host_public_allowed(host: str) -> None:
    assert is_blocked_host(host) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:169.254.169.254]/admin",
        "https://[fc00::1]/internal",
    ],
)
def test_validate_url_rejects_bracketed_ipv6_internal(url: str) -> None:
    with pytest.raises(BlockedHost):
        validate_url(url)


def test_validate_url_allows_public_ipv6() -> None:
    assert validate_url("https://[2001:4860:4860::8888]/") is not None


@pytest.mark.parametrize(
    ("allowed_entry", "host"),
    [
        ("::1", "[::1]"),
        ("[::1]", "[::1]"),
        ("127.0.0.1", "[::ffff:127.0.0.1]"),
        ("127.0.0.1", "[::ffff:7f00:1]"),
        ("FC00::1", "[fc00::1]"),
    ],
)
def test_is_blocked_host_allowed_hosts_normalize_brackets_and_mapped(
    monkeypatch: pytest.MonkeyPatch, allowed_entry: str, host: str
) -> None:
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", [allowed_entry])
    assert is_blocked_host(host) is False


@pytest.mark.parametrize("host", ["LOCALHOST", "LocalHost", "localhost"])
def test_is_blocked_host_blocked_hosts_case_insensitive(host: str) -> None:
    assert is_blocked_host(host) is True
