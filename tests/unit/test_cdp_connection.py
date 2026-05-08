from __future__ import annotations

import socket
from typing import Any, cast

import pytest
from playwright.async_api import Playwright

import skyvern.webeye.cdp_connection as cdp_connection
from skyvern.webeye.cdp_connection import (
    build_cdp_connection_candidates,
    build_chrome_inspect_ws_url,
    connect_over_cdp_with_diagnostics,
    resolve_host_docker_internal_url,
)


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


def test_build_chrome_inspect_ws_url_uses_browser_ws_endpoint() -> None:
    assert (
        build_chrome_inspect_ws_url("http://host.docker.internal:9222/")
        == "ws://host.docker.internal:9222/devtools/browser"
    )


def test_build_cdp_connection_candidates_includes_inspect_ws_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: int, family: socket.AddressFamily) -> list[Any]:
        assert host == "host.docker.internal"
        assert port == 9222
        assert family == socket.AF_INET
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.65.254", port))]

    monkeypatch.setattr(cdp_connection.socket, "getaddrinfo", fake_getaddrinfo)

    candidates = list(build_cdp_connection_candidates("http://host.docker.internal:9222/", {"x-api-key": "key"}))

    assert [(candidate.label, candidate.url, candidate.headers) for candidate in candidates] == [
        ("resolved host.docker.internal IPv4", "http://192.168.65.254:9222/", {"x-api-key": "key"}),
        (
            "chrome://inspect WebSocket endpoint with loopback Host header",
            "ws://host.docker.internal:9222/devtools/browser",
            {"x-api-key": "key", "Host": "127.0.0.1:9222"},
        ),
        (
            "chrome://inspect WebSocket endpoint",
            "ws://host.docker.internal:9222/devtools/browser",
            {"x-api-key": "key"},
        ),
        (
            "resolved chrome://inspect WebSocket endpoint",
            "ws://192.168.65.254:9222/devtools/browser",
            {"x-api-key": "key"},
        ),
    ]


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


@pytest.mark.asyncio
async def test_connect_over_cdp_retries_inspect_ws_after_http_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_browser = object()

    class FakeChromium:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        async def connect_over_cdp(self, url: str, *, headers: dict[str, str] | None = None) -> object:
            self.calls.append((url, headers))
            if len(self.calls) < 3:
                raise Exception(
                    "BrowserType.connect_over_cdp: Unexpected status 404 when connecting to "
                    f"{url.rstrip('/')}/json/version/."
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
        (
            "ws://host.docker.internal:9222/devtools/browser",
            {"x-api-key": "test-key", "Host": "127.0.0.1:9222"},
        ),
    ]
