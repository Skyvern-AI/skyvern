import socket

import pytest
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.exceptions import BlockedHost, SkyvernHTTPException, UnresolvableHost
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import TaskRequest
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.schemas.run_blocks import BaseRunBlockRequest
from skyvern.schemas.runs import BlockRunRequest, TaskRunRequest, WorkflowRunRequest
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest
from skyvern.utils.url_validators import (
    encode_url,
    is_blocked_host,
    validate_fetch_url,
    validate_redirect_url,
    validate_url,
    validate_webhook_url,
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


def test_is_blocked_host_does_not_treat_worker_dns_failure_as_a_policy_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fails_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise OSError("dns unavailable")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", fails_dns)

    assert is_blocked_host("public.example.test", resolve_dns=True) is False


@pytest.mark.parametrize("host", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "localhost"])
def test_is_blocked_host_still_refuses_internal_targets_without_resolving(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    def unexpected_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError("internal targets must be refused before DNS is consulted")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", unexpected_dns)

    assert is_blocked_host(host, resolve_dns=True) is True


def test_validate_fetch_url_blocks_hostname_resolving_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_to_private(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.42", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_to_private)

    with pytest.raises(BlockedHost):
        validate_fetch_url("https://evil.example.test/file.pdf")


@pytest.mark.parametrize("blocked_host", ["127.0.0.1", "10.0.0.5", "169.254.169.254"])
def test_validate_fetch_url_checks_blocked_host_when_url_is_too_long(
    blocked_host: str,
) -> None:
    url = f"http://{blocked_host}/resource?payload=" + "x" * 2100
    assert len(url) > 2083

    with pytest.raises(BlockedHost) as exc_info:
        validate_fetch_url(url)

    assert type(exc_info.value) is BlockedHost


@pytest.mark.parametrize("url", ["ftp://public.example.test/file", "chrome://settings", "gopher://host/x"])
def test_validate_fetch_url_refuses_nonhttp_scheme_without_dns(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    def unexpected_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError("non-http(s) schemes must be refused before DNS is consulted")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", unexpected_dns)

    with pytest.raises(SkyvernHTTPException):
        validate_fetch_url(url)


@pytest.mark.parametrize("blocked_host", ["169.254.169.254", "127.0.0.1", "10.0.0.5"])
def test_validate_fetch_url_checks_blocked_host_behind_backslash_authority(
    blocked_host: str,
) -> None:
    # A browser reads the backslash as a separator and navigates to blocked_host, so the
    # over-length fallback must not be fooled into reading an unrelated host.
    url = f"http://{blocked_host}\\.example.com/latest/meta-data/#" + "x" * 2100
    assert len(url) > 2083

    with pytest.raises(BlockedHost) as exc_info:
        validate_fetch_url(url)

    assert type(exc_info.value) is BlockedHost


def test_validate_fetch_url_allows_long_url_with_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_to_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_to_public)
    url = "https://public.example.test/callback?response=" + "x" * 2100
    assert len(url) > 2100

    assert validate_fetch_url(url) == url


@pytest.mark.parametrize("url", ["http://localhost:8000/", "http://127.0.0.1:3000/"])
def test_validate_fetch_url_blocks_localhost_and_loopback_without_allowed_hosts(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", [])

    with pytest.raises(BlockedHost):
        validate_fetch_url(url)


def test_validate_fetch_url_allows_localhost_in_allowed_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_to_loopback(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port or 0))]

    monkeypatch.setattr(settings, "ALLOWED_HOSTS", ["localhost"])
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_to_loopback)
    url = "http://localhost:8000/"

    assert validate_fetch_url(url) == url


@pytest.mark.parametrize("url", ["http://169.254.169.254/", "http://10.0.0.5/"])
def test_validate_fetch_url_blocks_metadata_and_private_hosts(url: str) -> None:
    with pytest.raises(BlockedHost):
        validate_fetch_url(url)


def test_validate_fetch_url_blocks_localhost_resolving_to_private_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_to_private(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_to_private)

    with pytest.raises(BlockedHost):
        validate_fetch_url("http://localhost:8000/")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://public.example.test/file",
        "chrome://settings",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_validate_fetch_url_refuses_other_schemes_without_resolving(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """A scheme we refuse outright must not emit a DNS query for its host.

    Resolving decides nothing for these, and it turns every rejected URL into a lookup of an
    attacker-supplied name.
    """
    resolved: list[str] = []

    def record(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        resolved.append(host)
        raise socket.gaierror("should not be called")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", record)

    with pytest.raises(SkyvernHTTPException):
        validate_fetch_url(url)

    assert resolved == []


def test_validate_fetch_url_still_resolves_a_backslash_authority_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The http backslash-authority vector still resolves; only refused schemes skip DNS."""

    def resolves_internal(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_internal)

    with pytest.raises(BlockedHost):
        validate_fetch_url("http://sneaky.example.test\\@public.example.test/")


def test_validate_fetch_url_fails_closed_on_dns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fails_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise OSError("dns unavailable")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", fails_dns)

    with pytest.raises(BlockedHost) as exc_info:
        validate_fetch_url("https://unresolvable.example.test/file.pdf")

    assert type(exc_info.value) is UnresolvableHost


def test_validate_fetch_url_fails_closed_without_resolved_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_without_answers(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_without_answers)

    with pytest.raises(UnresolvableHost):
        validate_fetch_url("https://unresolvable.example.test/file.pdf")


def test_validate_url_does_not_resolve_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError("general URL validation should not resolve DNS")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", unexpected_dns)

    assert validate_url("https://webhook.example.com/receive") is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://service-123.us-east-1.elb.amazonaws.com/webhook",
        "https://service-123.elb.us-east-1.amazonaws.com/webhook",
        "https://dualstack.service-123.elb.us-east-1.amazonaws.com/webhook",
        "https://service-123.elb.cn-north-1.amazonaws.com.cn/webhook",
    ],
)
def test_validate_webhook_url_rejects_raw_aws_load_balancer_hosts(url: str) -> None:
    with pytest.raises(SkyvernHTTPException, match="stable custom hostname"):
        validate_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://webhook.example.com/receive",
        "https://elb.example.com/receive",
        "https://service.amazonaws.com/receive",
        "https://elb.s3.amazonaws.com/object",
        "https://bucket.elb.s3.amazonaws.com/object",
        "https://service.elb.us-east-1.amazonaws.com.example.com/receive",
    ],
)
def test_validate_webhook_url_allows_stable_hosts(url: str) -> None:
    assert validate_webhook_url(url) == url


def test_validate_url_still_allows_raw_aws_load_balancer_hosts_for_navigation() -> None:
    url = "https://service-123.elb.us-east-1.amazonaws.com/page"
    assert validate_url(url) == url


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            TaskRequest,
            {
                "url": "https://example.com",
                "webhook_callback_url": "https://service-123.elb.us-east-1.amazonaws.com/hook",
            },
        ),
        (
            TaskV2Request,
            {
                "user_prompt": "test",
                "webhook_callback_url": "https://service-123.elb.us-east-1.amazonaws.com/hook",
            },
        ),
        (BaseRunBlockRequest, {"webhook_url": "https://service-123.elb.us-east-1.amazonaws.com/hook"}),
        (
            BlockRunRequest,
            {
                "workflow_id": "wpid_test",
                "block_labels": ["block_1"],
                "webhook_url": "https://service-123.elb.us-east-1.amazonaws.com/hook",
            },
        ),
    ],
)
def test_persisted_webhook_request_models_reject_raw_aws_load_balancer_hosts(
    model: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(SkyvernHTTPException, match="stable custom hostname"):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload", "field"),
    [
        (
            TaskRunRequest,
            {"prompt": "test", "webhook_url": "https://service-123.elb.us-east-1.amazonaws.com/hook"},
            "webhook_url",
        ),
        (
            WorkflowRunRequest,
            {
                "workflow_id": "wpid_test",
                "webhook_url": "https://service-123.elb.us-east-1.amazonaws.com/hook",
            },
            "webhook_url",
        ),
        (
            WorkflowRequestBody,
            {"webhook_callback_url": "https://service-123.elb.us-east-1.amazonaws.com/hook"},
            "webhook_callback_url",
        ),
        (
            WorkflowCreateYAMLRequest,
            {
                "title": "test",
                "webhook_callback_url": "https://service-123.elb.us-east-1.amazonaws.com/hook",
                "workflow_definition": {"parameters": [], "blocks": []},
            },
            "webhook_callback_url",
        ),
    ],
)
def test_models_used_for_persisted_reads_allow_legacy_raw_load_balancer_hosts(
    model: type[BaseModel], payload: dict[str, object], field: str
) -> None:
    parsed = model.model_validate(payload)
    assert getattr(parsed, field) == payload[field]


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
