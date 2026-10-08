from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing
from playwright.async_api import Playwright

import skyvern.webeye.browser_factory as browser_factory
import skyvern.webeye.cdp_connection as cdp_connection
from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.schemas.runs import TaskRunRequest
from skyvern.webeye import browser_acquisition_sample as sample_mod
from skyvern.webeye.cdp_connection import (
    REDACTED,
    build_cdp_connect_headers,
    build_cdp_connection_candidates,
    connect_over_cdp_with_diagnostics,
    merge_cdp_connect_headers,
    parse_default_cdp_connect_headers,
    resolve_host_docker_internal_url,
)
from skyvern.webeye.cdp_retry import connect_over_cdp_with_retry


def test_build_cdp_connect_headers_uses_host_header() -> None:
    assert build_cdp_connect_headers(" 127.0.0.1:9222 ") == {"Host": "127.0.0.1:9222"}


def test_build_cdp_connect_headers_ignores_empty_host_header() -> None:
    assert build_cdp_connect_headers(None) is None
    assert build_cdp_connect_headers(" ") is None


def test_parse_default_cdp_connect_headers_empty() -> None:
    assert parse_default_cdp_connect_headers(None) == {}
    assert parse_default_cdp_connect_headers("") == {}


def test_parse_default_cdp_connect_headers_returns_dict() -> None:
    assert parse_default_cdp_connect_headers('{"x-api-key": "secret"}') == {"x-api-key": "secret"}


def test_parse_default_cdp_connect_headers_ignores_invalid_json() -> None:
    assert parse_default_cdp_connect_headers("not-json") == {}


def test_parse_default_cdp_connect_headers_ignores_non_object_json() -> None:
    assert parse_default_cdp_connect_headers('["x-api-key"]') == {}
    assert parse_default_cdp_connect_headers('"raw-string"') == {}


def test_parse_default_cdp_connect_headers_skips_non_string_values() -> None:
    assert parse_default_cdp_connect_headers('{"x-api-key": "secret", "x-bad": 42, "x-null": null}') == {
        "x-api-key": "secret",
    }


def test_merge_cdp_connect_headers_per_row_overrides_default() -> None:
    merged = merge_cdp_connect_headers(
        default_headers={"x-api-key": "env-secret", "x-shared": "env"},
        per_row_headers={"x-api-key": "row-secret", "x-row-only": "row"},
        managed_host_header={},
    )
    assert merged == {"x-api-key": "row-secret", "x-shared": "env", "x-row-only": "row"}


def test_merge_cdp_connect_headers_managed_host_always_wins() -> None:
    merged = merge_cdp_connect_headers(
        default_headers={"host": "env-host", "x-api-key": "env"},
        per_row_headers={"Host": "row-host", "x-api-key": "row"},
        managed_host_header={"Host": "127.0.0.1:9222"},
    )
    assert merged == {"Host": "127.0.0.1:9222", "x-api-key": "row"}


def test_merge_cdp_connect_headers_no_inputs() -> None:
    assert merge_cdp_connect_headers({}, None, {}) == {}


def test_merge_cdp_connect_headers_defaults_only() -> None:
    assert merge_cdp_connect_headers({"x-api-key": "env"}, None, {}) == {"x-api-key": "env"}


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


def test_build_cdp_connection_candidates_includes_resolved_host_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: int, family: socket.AddressFamily) -> list[Any]:
        assert host == "host.docker.internal"
        assert port == 9222
        assert family == socket.AF_INET
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.65.254", port))]

    monkeypatch.setattr(cdp_connection.socket, "getaddrinfo", fake_getaddrinfo)

    candidates = list(build_cdp_connection_candidates("http://host.docker.internal:9222/", {"x-api-key": "key"}))

    assert [(candidate.label, candidate.url, candidate.headers) for candidate in candidates] == [
        ("resolved host.docker.internal IPv4", "http://192.168.65.254:9222/", {"x-api-key": "key"}),
    ]


@pytest.mark.asyncio
async def test_connect_over_cdp_retries_resolved_host_with_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_browser = object()

    class FakeChromium:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | None, dict[str, str] | None]] = []

        async def connect_over_cdp(
            self,
            url: str,
            *,
            timeout: int | None = None,
            headers: dict[str, str] | None = None,
        ) -> object:
            self.calls.append((url, timeout, headers))
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
        timeout_ms=120000,
    )

    assert browser is expected_browser
    assert fake_playwright.chromium.calls == [
        ("http://host.docker.internal:9222/", 120000, headers),
        ("http://192.168.65.254:9222/", 120000, headers),
    ]


@pytest.mark.asyncio
async def test_create_cdp_connection_browser_passes_headers_to_configured_cdp_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None, dict[str, str] | None]] = []

    async def fake_connect_to_cdp_browser(
        playwright: Playwright,
        remote_browser_url: str,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        apply_download_behaviour: bool = False,
        validate_browser_address: bool = True,
    ) -> tuple[object, object, object]:
        calls.append((remote_browser_url, extra_http_headers, cdp_connect_headers))
        return object(), object(), object()

    monkeypatch.setattr(browser_factory.settings, "BROWSER_TYPE", "chromium-headful")
    monkeypatch.setattr(browser_factory.settings, "BROWSER_REMOTE_DEBUGGING_URL", "http://browser.example:9222")
    monkeypatch.setattr(browser_factory, "_connect_to_cdp_browser", fake_connect_to_cdp_browser)

    await browser_factory._create_cdp_connection_browser(
        cast(Playwright, object()),
        extra_http_headers={"user-agent": "test"},
        cdp_connect_headers={"x-api-key": "secret"},
    )

    assert calls == [
        ("http://browser.example:9222", {"user-agent": "test"}, {"x-api-key": "secret"}),
    ]


@pytest.mark.asyncio
async def test_connect_over_cdp_accepts_direct_websocket_with_default_timeout() -> None:
    expected_browser = object()

    class FakeChromium:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | None, dict[str, str] | None]] = []

        async def connect_over_cdp(
            self,
            url: str,
            *,
            timeout: int | None = None,
            headers: dict[str, str] | None = None,
        ) -> object:
            self.calls.append((url, timeout, headers))
            return expected_browser

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

    fake_playwright = FakePlaywright()

    browser = await connect_over_cdp_with_diagnostics(
        cast(Playwright, fake_playwright),
        "ws://host.docker.internal:9223/devtools/browser/abc",
    )

    assert browser is expected_browser
    assert fake_playwright.chromium.calls == [
        ("ws://host.docker.internal:9223/devtools/browser/abc", 30_000, None),
    ]


@pytest.mark.asyncio
async def test_connect_over_cdp_with_diagnostics_validates_browser_address_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "prod")
    connect_over_cdp = AsyncMock()
    playwright = cast(Playwright, SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp)))

    with pytest.raises(BlockedHost):
        await connect_over_cdp_with_diagnostics(playwright, "ws://10.0.0.5:9222")

    connect_over_cdp.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_remote_browser_address_connects_with_caller_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.utils.url_validators.socket.getaddrinfo",
        lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))],
    )
    browser_address = "wss://browser.example.test/devtools/browser/id"
    request = TaskRunRequest(
        prompt="run",
        browser_address=browser_address,
        cdp_connect_headers={"X-Provider-Auth": "provider-value"},
    )
    expected_browser = object()
    connect_over_cdp = AsyncMock(return_value=expected_browser)
    playwright = cast(Playwright, SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp)))

    browser = await connect_over_cdp_with_diagnostics(
        playwright,
        request.browser_address or "",
        headers=request.cdp_connect_headers,
    )

    assert browser is expected_browser
    connect_over_cdp.assert_awaited_once_with(
        browser_address,
        timeout=30_000,
        headers={"X-Provider-Auth": "provider-value"},
    )


@pytest.mark.asyncio
async def test_remote_browser_address_rejects_hostname_resolving_to_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(
        "skyvern.utils.url_validators.socket.getaddrinfo",
        lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.42", 0))],
    )
    request = TaskRunRequest(
        prompt="run",
        browser_address="wss://localhost.attacker.test/devtools/browser/id",
    )
    connect_over_cdp = AsyncMock()
    playwright = cast(Playwright, SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp)))

    with pytest.raises(BlockedHost):
        await connect_over_cdp_with_retry(
            playwright,
            request.browser_address or "",
            validate_browser_address=True,
        )

    connect_over_cdp.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_loopback_browser_address_connects_without_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    getaddrinfo = MagicMock(side_effect=AssertionError("local loopback connection must not resolve DNS"))
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", getaddrinfo)
    expected_browser = object()
    connect_over_cdp = AsyncMock(return_value=expected_browser)
    playwright = cast(Playwright, SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp)))

    browser = await connect_over_cdp_with_retry(
        playwright,
        "ws://localhost:9222",
        validate_browser_address=True,
    )

    assert browser is expected_browser
    getaddrinfo.assert_not_called()


@pytest.mark.asyncio
async def test_connect_over_cdp_accepts_direct_websocket_with_host_header() -> None:
    expected_browser = object()

    class FakeChromium:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | None, dict[str, str] | None]] = []

        async def connect_over_cdp(
            self,
            url: str,
            *,
            timeout: int | None = None,
            headers: dict[str, str] | None = None,
        ) -> object:
            self.calls.append((url, timeout, headers))
            return expected_browser

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

    fake_playwright = FakePlaywright()
    headers = {"Host": "127.0.0.1:9222"}

    browser = await connect_over_cdp_with_diagnostics(
        cast(Playwright, fake_playwright),
        "ws://host.docker.internal:9223/devtools/browser/abc",
        headers=headers,
        timeout_ms=120000,
    )

    assert browser is expected_browser
    assert fake_playwright.chromium.calls == [
        ("ws://host.docker.internal:9223/devtools/browser/abc", 120000, headers),
    ]


@pytest.mark.asyncio
async def test_cdp_connect_never_logs_a_credential_bearing_remote_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKY-13287: `remote_browser_url` here is caller-supplied — a routed session address whose
    query carries the session token, or a vendor endpoint carrying an api key. Both the connect
    line and the launched line have to be readable without handing the reader that credential."""
    secret = "pbs_routed.minted-secret"
    routed_url = f"wss://session-router.skyvern.com/pbs_routed?token={secret}"

    fake_context = object()
    fake_browser = cast(Any, SimpleNamespace(contexts=[fake_context]))

    async def fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return fake_browser

    monkeypatch.setattr(browser_factory, "_connect_over_cdp_with_diagnostics", fake_connect)

    with structlog.testing.capture_logs() as logs:
        await browser_factory._connect_to_cdp_browser(cast(Playwright, object()), routed_url)

    rendered = json.dumps(logs, default=str)
    assert secret not in rendered
    assert "minted-secret" not in rendered
    # Redacted, not dropped: the session it dialed still has to be identifiable.
    assert "pbs_routed" in rendered
    assert REDACTED in rendered


class TestCdpConnectAcquireMode:
    """_create_cdp_connection_browser resolves the acquisition mode at its actual dispatch: dialing a
    caller address or a configured/already-running remote endpoint is attach; only launching a new
    Chrome process is create. The fixed cdp-connect worker (no per-run address) must not be mislabeled
    create by the pre-dispatch heuristic."""

    @pytest.mark.asyncio
    async def test_caller_address_marks_attach(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            browser_factory, "_connect_to_cdp_browser", AsyncMock(return_value=(MagicMock(), MagicMock(), None))
        )
        scope = sample_mod.begin_browser_acquisition_sample()
        try:
            await browser_factory._create_cdp_connection_browser(MagicMock(), browser_address="ws://caller:9222")
            assert sample_mod.current_browser_acquisition_sample().resolved_acquire_mode == "attach"
        finally:
            sample_mod.close_browser_acquisition_sample(scope)

    @pytest.mark.asyncio
    async def test_configured_remote_endpoint_marks_attach(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(browser_factory.settings, "BROWSER_TYPE", "cdp-connect")
        monkeypatch.setattr(browser_factory.settings, "CHROME_EXECUTABLE_PATH", "")  # no launch → configured remote
        monkeypatch.setattr(
            browser_factory, "_connect_to_cdp_browser", AsyncMock(return_value=(MagicMock(), MagicMock(), None))
        )
        scope = sample_mod.begin_browser_acquisition_sample()
        try:
            await browser_factory._create_cdp_connection_browser(MagicMock())
            assert sample_mod.current_browser_acquisition_sample().resolved_acquire_mode == "attach"
        finally:
            sample_mod.close_browser_acquisition_sample(scope)

    @pytest.mark.asyncio
    async def test_launching_new_chrome_marks_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(browser_factory.settings, "BROWSER_TYPE", "cdp-connect")
        monkeypatch.setattr(browser_factory.settings, "CHROME_EXECUTABLE_PATH", "/usr/bin/chromium")
        monkeypatch.setattr(browser_factory, "_is_port_in_use", lambda port: False)  # free → launch
        monkeypatch.setattr(browser_factory, "_is_chrome_running", lambda: False)
        monkeypatch.setattr(browser_factory.os.path, "exists", lambda p: True)
        monkeypatch.setattr(browser_factory, "is_valid_chromium_user_data_dir", lambda p: True)  # skip copytree
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None  # process stayed alive
        monkeypatch.setattr(browser_factory.subprocess, "Popen", lambda *a, **k: fake_proc)
        monkeypatch.setattr(browser_factory.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            browser_factory, "_connect_to_cdp_browser", AsyncMock(return_value=(MagicMock(), MagicMock(), None))
        )
        scope = sample_mod.begin_browser_acquisition_sample()
        try:
            await browser_factory._create_cdp_connection_browser(MagicMock())
            assert sample_mod.current_browser_acquisition_sample().resolved_acquire_mode == "create"
        finally:
            sample_mod.close_browser_acquisition_sample(scope)


class TestDiagnosticsDialRetryRecording:
    """connect_over_cdp_with_diagnostics records each dial (first URL + every resolved-address
    fallback) as a CDP-connect attempt, so a first-try acquisition sample reports the retries this
    helper makes — its retry loop never routes through connect_over_cdp_with_retry."""

    @staticmethod
    def _resolving_getaddrinfo(host: str, port: int, family: socket.AddressFamily) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.65.254", port))]

    @pytest.mark.asyncio
    async def test_ipv4_fallback_dial_counts_as_retry_defeating_first_try(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeChromium:
            def __init__(self) -> None:
                self.calls = 0

            async def connect_over_cdp(self, url: str, *, timeout: int | None = None, headers: Any = None) -> object:
                self.calls += 1
                if self.calls == 1:
                    raise Exception("Unexpected status 500 when connecting to http://host.docker.internal:9222/")
                return object()

        pw = SimpleNamespace(chromium=FakeChromium())
        monkeypatch.setattr(cdp_connection.socket, "getaddrinfo", self._resolving_getaddrinfo)
        scope = sample_mod.begin_browser_acquisition_sample()
        try:
            await connect_over_cdp_with_diagnostics(
                cast(Playwright, pw), "http://host.docker.internal:9222/", validate_browser_address=False
            )
            sample = sample_mod.current_browser_acquisition_sample()
            assert sample is not None
            assert sample.cdp_connect_attempts == 2  # first dial failed, IPv4 fallback succeeded
            assert sample_mod.is_first_try_success(sample, outcome_success=True) is False
        finally:
            sample_mod.close_browser_acquisition_sample(scope)

    @pytest.mark.asyncio
    async def test_first_dial_success_is_one_attempt_first_try_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=object())))
        scope = sample_mod.begin_browser_acquisition_sample()
        try:
            await connect_over_cdp_with_diagnostics(
                cast(Playwright, pw), "http://host.docker.internal:9222/", validate_browser_address=False
            )
            sample = sample_mod.current_browser_acquisition_sample()
            assert sample is not None
            assert sample.cdp_connect_attempts == 1
            assert sample_mod.is_first_try_success(sample, outcome_success=True) is True
        finally:
            sample_mod.close_browser_acquisition_sample(scope)
