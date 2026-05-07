from __future__ import annotations

import socket
from typing import Any, cast

import pytest
from playwright.async_api import Playwright

import skyvern.webeye.cdp_connection as cdp_connection
from skyvern.webeye.cdp_connection import connect_over_cdp_with_diagnostics, resolve_host_docker_internal_url


def test_resolve_host_docker_internal_url_uses_resolved_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: int, family: socket.AddressFamily) -> list[Any]:
        assert host == "host.docker.internal"
        assert port == 9222
        assert family == socket.AF_INET
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.65.254", port))]

    monkeypatch.setattr(cdp_connection.socket, "getaddrinfo", fake_getaddrinfo)

    assert resolve_host_docker_internal_url("http://host.docker.internal:9222/") == "http://192.168.65.254:9222/"


def test_resolve_host_docker_internal_url_ignores_non_docker_hosts() -> None:
    assert resolve_host_docker_internal_url("http://127.0.0.1:9222/") is None


@pytest.mark.asyncio
async def test_connect_over_cdp_retries_resolved_host_with_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_browser = object()

    class FakeChromium:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        async def connect_over_cdp(self, url: str, *, headers: dict[str, str] | None = None) -> object:
            self.calls.append((url, headers))
            if len(self.calls) == 1:
                raise Exception(
                    "BrowserType.connect_over_cdp: Unexpected status 500 when connecting to "
                    "http://host.docker.internal:9222/json/version/."
                )
            return expected_browser

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

    def fake_getaddrinfo(host: str, port: int, family: socket.AddressFamily) -> list[Any]:
        assert host == "host.docker.internal"
        assert port == 9222
        assert family == socket.AF_INET
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.65.254", port))]

    monkeypatch.setattr(cdp_connection.socket, "getaddrinfo", fake_getaddrinfo)
    fake_playwright = FakePlaywright()
    headers = {"x-api-key": "test-key"}

    browser = await connect_over_cdp_with_diagnostics(
        cast(Playwright, fake_playwright),
        "http://host.docker.internal:9222/",
        headers=headers,
    )

    assert browser is expected_browser
    assert fake_playwright.chromium.calls == [
        ("http://host.docker.internal:9222/", headers),
        ("http://192.168.65.254:9222/", headers),
    ]
