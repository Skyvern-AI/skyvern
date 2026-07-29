import socket

import pytest

from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.utils.url_validators import (
    encode_url,
    is_blocked_host,
    validate_fetch_url,
    validate_redirect_url,
    validate_url,
)


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
        "100.100.100.200",
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
    "url",
    ["http://2130706433/", "http://0x7f000001/", "http://017700000001/", "http://127.1/", "http://0/"],
)
def test_validate_fetch_url_rejects_nonstandard_ip_encodings(url: str) -> None:
    with pytest.raises(BlockedHost):
        validate_fetch_url(url)


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


@pytest.mark.parametrize(
    "blocked_ip",
    [
        "127.0.0.2",
        "10.0.0.2",
        "172.16.0.2",
        "192.168.0.2",
        "169.254.0.2",
        "100.64.0.2",
        "100.100.100.200",
        "169.254.169.254",
        "::1",
        "fc00::2",
        "fd00:ec2::254",
    ],
)
def test_is_blocked_host_rejects_any_blocked_dns_answer(monkeypatch: pytest.MonkeyPatch, blocked_ip: str) -> None:
    family = socket.AF_INET6 if ":" in blocked_ip else socket.AF_INET

    def resolves_with_blocked_answer(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0)),
            (family, socket.SOCK_STREAM, 0, "", (blocked_ip, port or 0)),
        ]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_with_blocked_answer)

    assert is_blocked_host("public.example.test", resolve_dns=True) is True


def test_is_blocked_host_allows_public_dns_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2606:2800:220:1:248:1893:25c8:1946", port or 0)),
        ]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)

    assert is_blocked_host("public.example.test", resolve_dns=True) is False


def test_validate_fetch_url_blocks_hostname_resolving_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_to_private(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.42", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_to_private)

    with pytest.raises(BlockedHost):
        validate_fetch_url("https://evil.example.test/file.pdf")


def test_validate_fetch_url_fails_closed_on_dns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fails_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise OSError("dns unavailable")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", fails_dns)

    with pytest.raises(BlockedHost):
        validate_fetch_url("https://unresolvable.example.test/file.pdf")


def test_validate_url_does_not_resolve_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError("general URL validation should not resolve DNS")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", unexpected_dns)

    assert validate_url("https://webhook.example.com/receive") is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://kubernetes.default.svc/api",
        "https://my-service.namespace.svc.cluster.local/api",
        "https://internal.local/api",
    ],
)
def test_validate_url_blocks_internal_hostnames(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError("internal hostname should be blocked before DNS")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", unexpected_dns)

    with pytest.raises(BlockedHost):
        validate_url(url)


def test_is_blocked_host_allows_public_svc_subdomain() -> None:
    assert is_blocked_host("api.svc.example.com") is False


def test_validate_redirect_url_rejects_private_redirect_target() -> None:
    with pytest.raises(BlockedHost):
        validate_redirect_url("https://example.com/file.pdf", "http://169.254.169.254/latest/meta-data")
