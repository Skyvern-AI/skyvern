import ipaddress
import socket

import pytest

PUBLIC_TEST_IP = "93.184.216.34"


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to a public address, so code behind the SSRF validator runs without network DNS."""
    real_getaddrinfo = socket.getaddrinfo

    def _resolve(host: str, port: object = None, *args: object, **kwargs: object) -> list:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_TEST_IP, port or 0))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", _resolve)


_PROXY_ENV_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


@pytest.fixture
def no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear HTTP(S)_PROXY and friends, which would otherwise route pinned requests through a developer's proxy."""
    for name in _PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
