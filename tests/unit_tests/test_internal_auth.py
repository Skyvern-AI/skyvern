from starlette.requests import Request

from skyvern.forge.sdk.routes.internal_auth import _is_local_request


def _make_request(host: str | None) -> Request:
    scope = {
        "type": "http",
        "client": (host, 12345) if host else None,
        "headers": [],
        "method": "GET",
        "path": "/",
        "scheme": "http",
    }
    return Request(scope)


def test_is_local_request_returns_false_for_public_ip() -> None:
    request = _make_request("8.8.8.8")  # public IPv4 address
    assert _is_local_request(request) is False


def test_is_local_request_accepts_loopback() -> None:
    request = _make_request("127.0.0.1")
    assert _is_local_request(request) is True


def test_is_local_request_accepts_ipv6_loopback() -> None:
    request = _make_request("::1")
    assert _is_local_request(request) is True


def test_is_local_request_rejects_private_networks() -> None:
    # RFC1918 private addresses must NOT be treated as local: trusting them lets
    # a private-network caller bypass the localhost gate on these key-minting
    # internal routes. See Skyvern-AI/skyvern#6890.
    request = _make_request("192.168.1.20")
    assert _is_local_request(request) is False


def test_is_local_request_rejects_docker_bridge_address() -> None:
    # Exact reproduction address from issue #6890.
    request = _make_request("172.17.0.2")
    assert _is_local_request(request) is False


def test_is_local_request_handles_missing_client() -> None:
    request = _make_request(None)
    assert _is_local_request(request) is False
